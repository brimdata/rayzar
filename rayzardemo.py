#!/usr/bin/env python3
#
# Demonstration of using zar, zq, and zql with Ray.
#
# Example usage:
#
# ./rayzardemo.py \
#    --find=":ip=192.168.0.51" \
#    --filter="_path=weird id.orig_h=192.168.0.51 | count() by id.orig_h,id.resp_h" \
#    --merge="sum(count) by id.orig_h,id.resp_h" \
#    | zq -t -
#
#

import argparse
import os
import ray
import subprocess

zqexec = 'zq'
zarexec = 'zar'

parser = argparse.ArgumentParser()
parser.add_argument("--zq", help="zq exec path", default=zqexec)
parser.add_argument("--zar", help="zar exec path", default=zarexec)
parser.add_argument("--zarroot", help="zar root path", default='')
parser.add_argument("--find", help="zar find query")
parser.add_argument("--filter", help="data zql filter")
parser.add_argument("--merge", help="data zql merge")
parser.add_argument("-t", "--text", help="output tzng", action='store_true')

@ray.remote
def zq(zql, zng):
    # Applies a zql expression to ZNG data.
    p = subprocess.run(args=[zqexec, zql, '-'], stdout=subprocess.PIPE, input=zng)
    return p.stdout

@ray.remote
def load(location, filter=None):
    # Loads zng data from the given location, optionally transforming it with a zql expression.
    if filter == None:
        filter = "*"
    args=[zqexec, filter, location]
    if location.endswith(".parquet"):
        # XXX parquet auto-detection not currently supported in zq
        args = [zqexec, '-i', 'parquet', filter, location]
    p = subprocess.run(args=args, stdout=subprocess.PIPE)
    return p.stdout

@ray.remote
class ZarArchive(object):
    def __init__(self, root):
        self.root = root

    def find(self, findq):
        args = [zarexec, 'find', '-R', self.root, findq]
        p = subprocess.run(args=args, stdout=subprocess.PIPE, text=True)
        locs = p.stdout.splitlines()
        return [load.remote(loc) for loc in locs]

@ray.remote
class ZngAggregator(object):
    def __init__(self):
        pass

    def aggregate(self, zql, obj_ids):
        # TODO: Distribute & pipeline the aggregation.
        objs = ray.get(obj_ids)
        b = bytearray()
        for o in objs:
            b.extend(o)
        zng = b
        return ray.get(zq.remote(zql, zng))

def textzng(zng):
    # Returns the textual format of a ZNG object.
    p = subprocess.run(args=[zqexec, '-t', '-'], stdout=subprocess.PIPE, input=zng)
    return p.stdout.decode("utf-8")

if __name__ == "__main__":
    args = parser.parse_args()
    zqexec = args.zq
    zarexec = args.zar

    ray.init()

    zar = ZarArchive.remote(args.zarroot)
    srcs = ray.get(zar.find.remote(args.find))
    xformed = [zq.remote(args.filter, src) for src in srcs]

    agg = ZngAggregator.remote()
    res = ray.get(agg.aggregate.remote(args.merge, xformed))
    if not args.text:
        os.sys.stdout.buffer.write(res)
    else:
        print(textzng(res))