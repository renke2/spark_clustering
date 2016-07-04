# -*- coding:utf-8 -*-

import os
import sys
import math
import numpy as np
from scipy.sparse import csr_matrix
from operator import add
sys.path.append('../')
from utils import local2mfs, now
from load_data import load_data_from_mongo, cut_words_local
from config import CLUSTER_NUM, CLUSTERING_ITER, INITIAL_ITER, RB_ITER, convergeDist, FILTER_SCALE
from pyspark import SparkContext
from pyspark.mllib.linalg import Vectors

def parse(line):
    tid, label = line.split('\t')
    return (tid, int(label))

def nmi(class_file, hashtag_file):
    fi = open('nmi.txt', 'w')
    sc = SparkContext(appName='PythonKMeans',master="mesos://219.224.134.211:5050")
    cl = sc.textFile(class_file)
    ha = sc.textFile(hashtag_file)
    cl_data = cl.map(parse).cache()
    ha_data = ha.map(parse).cache()
    cl_ha = cl_data.join(ha_data).cache()
    total = cl_ha.count()
    print total
    CLASS_NUM = 20
    parray = np.zeros([CLASS_NUM, CLASS_NUM])
    for c in range(CLASS_NUM):
        for h in range(CLASS_NUM):
            pcount = cl_ha.filter(lambda (tid, (cl, ha)): cl == (c + 1) and ha == (h + 1)).count()
            parray[c][h] = pcount * 1.0 / total
    c_array = parray.sum(axis=1) # row
    h_array = parray.sum(axis=0) # column
    print c_array
    print h_array

    px_py = np.zeros([CLASS_NUM, CLASS_NUM])
    for c in range(CLASS_NUM):
        for h in range(CLASS_NUM):
            px_py[c][h] = c_array[c] * h_array[h]
    results = parray * np.log(parray / px_py)
    results = np.nan_to_num(results)
    print results
    print >> fi, parray
    print >> fi, px_py
    print >> fi, results
    mi = results.sum()
    print mi

    logpx = c_array * np.log2(c_array)
    logpx = np.nan_to_num(logpx)
    hx = -logpx.sum()
    logpy = h_array * np.log2(h_array)
    logpy = np.nan_to_num(logpy)
    hy = -logpy.sum()
    nmi = 2 * mi / (hx + hy)
    print logpx
    print logpy
    print hx, hy, nmi
    fi.close()
    sc.stop()

def load_cut_to_rdd(input_file, result_file, cluster_num=CLUSTER_NUM, clu_iter=CLUSTERING_ITER,\
        ini_iter=INITIAL_ITER, rb_iter=RB_ITER, con_dist=convergeDist, filter_scale=FILTER_SCALE):
    sc = SparkContext(appName='PythonKMeans',master="mesos://219.224.134.211:5050")
    lines = sc.textFile(input_file)
    data = lines.flatMap(parseKV).cache()

    doc_term_tf = data.reduceByKey(add).cache()
    initial_num_doc = doc_term_tf.map(lambda ((tid, term), tf): tid).distinct().count()
    print 'initial_num_doc', initial_num_doc

    initial_term_idf = doc_term_tf.map(
            lambda ((tid, term), tf): (term, 1.0)
            ).reduceByKey(add)
    initial_num_term = initial_term_idf.count()
    print 'initial_num_term', initial_num_term
    idf_sum = initial_term_idf.values().sum()
    print 'idf_sum', idf_sum

    # filter
    idf_average = idf_sum  / (initial_num_term * filter_scale)
    filtered_term_idf = initial_term_idf.filter(
            lambda (term, idf): idf_average < idf < (idf_average * (filter_scale - 1))).cache()
    terms_list = filtered_term_idf.keys().collect()
    num_term = len(terms_list)
    print 'num_term', num_term

    tfidf_join = doc_term_tf.map(
            lambda ((tid, term), tf): (term, (tid, tf))).join(filtered_term_idf)  #(term, (tid, tf), df)
    id_tfidf = tfidf_join.map(lambda (term, ((tid, tf), df)): (tid, (terms_list.index(term), tf, df))).cache()
    num_doc = id_tfidf.keys().distinct().count()
    print 'num_doc', num_doc

    tfidf = id_tfidf.mapValues(
            lambda (index, tf, df) : (index, tf * math.log(float(num_doc) / (df+1))))

    doc_vec = tfidf.groupByKey().mapValues(lambda feature : csr_matrix(Vectors.sparse(num_term, feature).toArray())).cache()
    global_center = doc_vec.mapValues(
            lambda x: x / num_doc).values().reduce(add)
    g_length = vector_length(global_center)

    # initial 2-way clustering
    maximum_total_variance = 0
    best_kPoints = []
    print 'initial', now()
    for i in range(ini_iter):
        kPoints, tempDist, iter_count = clustering(doc_vec, K, con_dist, clu_iter)
        # evaluation
        cluster_variance, total_variance = cluster_evaluation(doc_vec, kPoints)

        # choose the best initial cluster
        if total_variance[0] > maximum_total_variance:
            maximum_total_variance = total_variance[0]
            best_kPoints = kPoints
            best_kPoints = kPoints
    global_distance = sum(cosine_dist(best_kPoints[x][1], global_center, best_kPoints[x][2], g_length) for x in range(len(best_kPoints)))

    f = open(result_file,'w')
    f.write(str(initial_num_doc)+"\t"+str(initial_num_term)+"\n")
    f.write(str(iter_count)+"\t"+str(num_doc)+"\t"+str(num_term)+"\n")
    for index in range(len(terms_list)):
        f.write(terms_list[index].encode('utf-8')+'\t')
    for (term, ((tid,tf), idf)) in tfidf_join.collect():
        f.write(term.encode('utf-8')+'\t'+str(tid)+'\t'+str(tf)+'\t'+str(idf)+'\n')
    print >> f, "%0.9f" % tempDist
    print >> f, "total_variance", total_variance[0], total_variance[1]
    print >> f, "global_dist", global_distance
    f.write("center:"+str(type(global_center))+"\n")
    for dim in global_center:
        f.write(str(dim)+"\t")
    f.write("\n")
    for i in range(len(best_kPoints)):
        f.write(str(i)+'\n')
        for unit in best_kPoints[i][1]:
            f.write("\t")
            f.write(str(unit))
        f.write("\n")
    for (index, (dist, num)) in cluster_variance.collect():
        f.write(str(index))
        f.write("\t")
        f.write(str(dist))
        f.write("\t")
        f.write(str(num))
        f.write("\n")
    f.close()
    #repeated bisect
    #choose cluster

    updated_dict = {}
    updated_points_dict = {}
    total_delta_variance = 0
    updated_dict[total_delta_variance] = [doc_vec]
    updated_points_dict[total_delta_variance] = [best_kPoints]

    print 'repeated', now()
    for j in range(2, cluster_num+1):
        if not (total_delta_variance in updated_dict):
            print "no cluster to divide"
            break

        best_cluster = updated_dict[total_delta_variance][0]
        global_best_kPoints = updated_points_dict[total_delta_variance][0]
        del updated_dict[total_delta_variance][0]
        del updated_points_dict[total_delta_variance][0]
        if len(updated_dict[total_delta_variance]) == 0:
            del updated_dict[total_delta_variance]
            del updated_points_dict[total_delta_variance]

        print 'cluster to divide', total_delta_variance, best_cluster

        closest = best_cluster.map(
                lambda (tid, feature):(closestPoint(feature, global_best_kPoints), (tid, feature))).cache()
        print 'total_count', closest.count()

        total_delta_variance = float("-inf") # clear to zero
        for key in updated_dict:
            if key > total_delta_variance:
                total_delta_variance = key

        for i in range(K):
            single_cluster = closest.filter(lambda (index, (tid, feature)): index == i).values().cache()
            print 'count', i, single_cluster.count()

            maximum_total_variance = 0
            best_kPoints = []
            initial_distance = cal_cluster_variance(single_cluster)
            for j in range(rb_iter):
                # clustering
                kPoints, tempDist, iter_count = clustering(single_cluster, K, con_dist, clu_iter)
                # evaluation
                cluster_variance, total_variance = cluster_evaluation(single_cluster, kPoints)

                if total_variance[0] > maximum_total_variance:
                    maximum_total_variance = total_variance[0]
                    best_kPoints = kPoints

            improvement = maximum_total_variance - initial_distance
            if improvement in updated_dict:
                updated_dict[improvement].append(single_cluster)
                updated_points_dict[improvement].append(best_kPoints)
            else:
                updated_dict[improvement] = [single_cluster]  # update dict
            print 'improvement', improvement, maximum_total_variance, initial_distance

            if improvement > total_delta_variance:
                total_delta_variance = improvement
                #print 'length', cluster_variance.count()

    count = 0
    fi = open('results/class', 'w')
    for key in updated_dict:
        for i in range(len(updated_dict[key])):
            count += 1
            print 'key, i', key, i
            per_cluster = updated_dict[key][i]

            total_similarity = cal_cluster_variance(per_cluster)
            f = open('results/cluster_'+str(count), 'w')
            print >> f, key, total_similarity

            results_list = per_cluster.values().reduce(add).toarray()
            for row in results_list:
                for index in range(len(row)):
                    value = row[index]
                    if value != 0:
                        f.write('('+str(index)+','+str(value)+')\t')
            f.write('\n')
            for (tid, feature) in per_cluster.collect():
                f.write(tid+'\t'+str(count)+'\n')
                fi.write(tid+'\t'+str(count)+'\n')
            """
            for (tid, feature) in per_cluster.collect():
                f.write(tid)
                for row in feature.toarray():
                    for unit in range(len(row)):
                        f.write('\t')
                        f.write(str(row[unit]))
                f.write('\n')
            """
            f.close()
    fi.close()

    sc.stop()
    return

if __name__ == '__main__':

    class_file = 'results/class'
    hashtag_file = '../data/no_hashtag.txt'
    print "start", now()
    nmi(class_file, hashtag_file)
    print "end", now()
