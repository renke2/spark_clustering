# -*- coding:utf-8 -*-

# use cosine_distance

import os
import math
import numpy as np
from operator import add
from utils import local2mfs, now
from load_data import load_data_from_mongo, cut_words_local
from pyspark import SparkContext
from pyspark.mllib.linalg import Vectors

AB_PATH = os.path.dirname(os.path.abspath(__file__))
print AB_PATH

def parseKV(line):
    tid, leng, term = line.split('\t')
    return ((tid, term), 1.0 / float(leng))

def cosine_dist(x, y):
    result = 0.0
    numerator = np.dot(x, y)
    denominator = math.sqrt(np.dot(x,x)) * math.sqrt(np.dot(y, y))
    result = numerator / denominator
    return result

def closestPoint(p, centers, withDist=False):
    bestIndex = 0
    closest = float("-inf")
    for i in range(len(centers)):
        tempDist = cosine_dist(p, centers[i][1])
        if tempDist > closest:
            closest = tempDist
            bestIndex = i
    if withDist == True:
        return (bestIndex, closest)
    else:
        return  bestIndex

def clustering(doc_vec, K, convergeDist, iter_count_limit):
    kPoints = doc_vec.takeSample(False, K)
    tempDist = 5.0
    iter_count = 0
    for i in range(len(kPoints)):
        kPoints[i] = list(kPoints[i])

    while tempDist > convergeDist and iter_count < iter_count_limit:
        iter_count += 1

        closest = doc_vec.map(
                lambda (tid, feature):(closestPoint(feature, kPoints), (tid, feature, 1)))
        pointState = closest.reduceByKey(
                lambda (x1, y1, z1), (x2, y2, z2): (-1, y1 + y2, z1 + z2))
        newPoints = pointState.map(
                lambda (x, (flag, y, z)): (x, y / z)).collect()

        tempDist = sum(cosine_dist(kPoints[x][1], y) for (x, y) in newPoints)

        for (x, y) in newPoints:
            kPoints[x][1] = y
    return kPoints, tempDist, iter_count

def cluster_evaluation(doc_vec, kPoints):

    closest = doc_vec.map(
            lambda (tid, feature):(closestPoint(feature, kPoints, True), (tid, feature, 1)))
    doc_variance = closest.map(
            lambda ((index, dist), (tid, feature, num)): (index, (dist, num)))
    cluster_variance = doc_variance.reduceByKey(lambda (x1,y1),(x2,y2):(x1+x2,y1+y2))
    total_variance = cluster_variance.map(
            lambda (index, (dist, num)): (dist, num)).reduce(lambda (x1,y1), (x2,y2):(x1+x2,y1+y2))

    return cluster_variance, total_variance

def cal_cluster_variance(doc_vec):
    cluster_center = doc_vec.mapValues(
            lambda x: (x, 1)).reduce(
            lambda (x1, (y1, z1)), (x2, (y2, z2)): (-1, (y1 + y2, z1 + z2)))
    _sum, _num = cluster_center[1]
    center = _sum / _num
    initial_distance = doc_vec.mapValues(
            lambda x:cosine_dist(x, center)).reduce(
            lambda (x1, y1), (x2, y2): (-1, (y1 + y2)))
    return initial_distance[1]

def load_cut_to_rdd(input_file, result_file):
    sc = SparkContext(appName='PythonKMeans',master="mesos://219.224.135.91:5050")
    lines = sc.textFile(input_file)
    data = lines.map(parseKV).cache()

    doc_term_tf = data.reduceByKey(add).cache()

    num_doc = doc_term_tf.map(lambda ((tid, term), tf): tid).distinct().count()
    terms_list = doc_term_tf.map(lambda ((tid, term), tf): term).distinct().collect()
    num_term = len(terms_list)

    term_idf = doc_term_tf.map(
            lambda ((tid, term), tf): (term, 1.0)
            ).reduceByKey(add).mapValues(lambda idf: math.log(float(num_doc) / (idf+1)))
    tfidf_join = doc_term_tf.map(
            lambda ((tid, term), tf): (term, (tid, tf))).join(term_idf)
    tfidf = tfidf_join.map(lambda (term, ((tid, tf), idf)): (tid, (terms_list.index(term), tf*idf)))

    doc_vec = tfidf.groupByKey().mapValues(lambda feature : Vectors.sparse(num_term, feature).toArray()).cache()
    global_center = doc_vec.mapValues(
            lambda x: x / num_doc).reduce(
            lambda (x1, y1), (x2, y2): (-1,y1 + y2))

    # initial 2-way clustering
    K = 2
    convergeDist = 0.01
    iter_count_limit = 10

    maximum_total_variance = 0
    best_kPoints = []
    print 'initial', now()
    for i in range(10):
        kPoints, tempDist, iter_count = clustering(doc_vec, K, convergeDist, iter_count_limit)
        # evaluation
        cluster_variance, total_variance = cluster_evaluation(doc_vec, kPoints)

        # choose the best initial cluster
        if total_variance[0] > maximum_total_variance:
            maximum_total_variance = total_variance[0]
            best_kPoints = kPoints
    global_distance = sum(cosine_dist(best_kPoints[x][1], global_center[1]) for x in range(len(best_kPoints)))

    """
    f = open(result_file,'w')
    f.write(str(iter_count)+"\t"+str(num_doc)+"\t"+str(num_term)+"\n")
    print >> f, "%0.9f" % tempDist
    print >> f, "total_variance", total_variance[0], total_variance[1]
    print >> f, "global_dist", global_distance
    f.write("center:"+"\t")
    for dim in global_center[1]:
        f.write(str(dim)+"\t")
    f.write("\n")
    for i in range(len(best_kPoints)):
        f.write(str(i))
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
    """
    #repeated bisect
    #choose cluster

    updated_dict = {}
    total_delta_variance = 0
    updated_dict[total_delta_variance] = doc_vec
    updated_cluster_variance = cluster_variance

    print 'repeated', now()
    for j in range(2, 6):
        if not (total_delta_variance in updated_dict):
            print "no cluster to divide"
            break

        # print 'cluster to divide', total_delta_variance, updated_dict[total_delta_variance]
        best_cluster = updated_dict[total_delta_variance]
        del updated_dict[total_delta_variance]
        closest = best_cluster.map(
                lambda (tid, feature):(closestPoint(feature, kPoints), (tid, feature))).cache()
        # print 'total_count', closest.count()

        total_delta_variance = float("-inf") # clear to zero
        for key in updated_dict:
            if key > total_delta_variance:
                total_delta_variance = key

        for i in range(K):
            single_cluster = closest.filter(lambda (index, (tid, feature)): index == i).values().cache()
            # print 'count', i, single_cluster.count()

            maximum_total_variance = 0
            initial_distance = cal_cluster_variance(single_cluster)
            for j in range(10):
                # clustering
                kPoints, tempDist, iter_count = clustering(single_cluster, K, convergeDist, iter_count_limit)
                # evaluation
                cluster_variance, total_variance = cluster_evaluation(single_cluster, kPoints)

                if total_variance[0] > maximum_total_variance:
                    maximum_total_variance = total_variance[0]

            improvement = maximum_total_variance - initial_distance
            updated_dict[improvement] = single_cluster # update dict
            # print 'improvement', improvement, maximum_total_variance, initial_distance

            if improvement > total_delta_variance:
                total_delta_variance = improvement
                updated_cluster_variance = cluster_variance
                # print 'length', cluster_variance.count()


    for key in updated_dict:
        print 'key', key

    sc.stop()
    return


if __name__ == "__main__":
    # topic = "APEC-微博"
    # print topic
    input_file = "data/source_chaijing.txt"
    output_file = "data/out_chaijing2.txt"
    result_file = "data/result_chaijing.txt"
    print "step1", now()
    # load_data_from_mongo(topic, input_file)

    print "step2", now()
    # cut_words_local(input_file, output_file)

    print "step3", now()
    load_cut_to_rdd(local2mfs(output_file), result_file)
    print "end", now()
