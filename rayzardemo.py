#!/usr/bin/env python3
#
# Demonstration of using zar, zq, and zql with Ray. A zar archive should already be
# created, and its location specified via a ZAR_ROOT environment variable.
#
# Example usage:
#
# ./rayzardemo.py \
#    --find=":ip=192.168.0.51" \
#    --filter="192.168.0.51 | count() by id.orig_h,id.resp_h" \
#    --merge="sum(count) as count by id.orig_h,id.resp_h" \
#    | zq -t -
#

import argparse
import os
import ray
import subprocess

# Default commands; override or specify locations via cli options.
zqexec = 'zq'
zarexec = 'zar'

def localzq(zql, zng):
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

# ArchiveSearch implements a ray actor to perform a zar search and provide
# a stream of hits via the next() method.
@ray.remote
class ArchiveSearch(object):
    def __init__(self, root, findq):
        # we run the find all at once up front... it would be better to stream
        # this if this weren't a demo
        args = [zarexec, 'find', '-R', root, findq]
        p = subprocess.run(args=args, stdout=subprocess.PIPE, text=True)
        self.locs = p.stdout.splitlines()

    def next(self):
        if len(self.locs) == 0:
            return None
        loc = self.locs[0]
        self.locs = self.locs[1:]
        return loc

# Aggregator implements a ray actor to aggregate results found by a zar search.
# This could be pandas or numpys logic, but for this simple example, we're storing
# the data as zng so we very inefficiently exec a process to combine the zng results.
@ray.remote
class Aggregator(object):
    def __init__(self):
        self.result = None

    def mix(self, location, merge_zql, filter=None):
        result = ray.get(load.remote(location, filter))
        if self.result == None:
            self.result = result
        else:
            zng = bytearray()
            zng.extend(self.result)
            zng.extend(result)
            self.result = localzq(merge_zql, zng)
        return True #XXX

    def result(self):
        return self.result

def textzng(zng):
    # Returns the textual format of a ZNG object.
    p = subprocess.run(args=[zqexec, '-t', '-'], stdout=subprocess.PIPE, input=zng)
    return p.stdout.decode("utf-8")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--zq", help="zq exec path", default=zqexec)
    parser.add_argument("--zar", help="zar exec path", default=zarexec)
    parser.add_argument("--zarroot", help="zar root path", default=os.getenv('ZAR_ROOT', ''))
    parser.add_argument("--find", help="zar find query")
    parser.add_argument("--filter", help="data zql filter")
    parser.add_argument("--merge", help="data zql merge")
    parser.add_argument("--nreaders", help="maximum numer of parallel zar readers", default=4)
    parser.add_argument("-t", "--text", help="output tzng", action='store_true')
    args = parser.parse_args()

    zqexec = args.zq
    zarexec = args.zar
    if args.zarroot == '':
        raise ValueError("missing zar root path; specify with --zarroot or via $ZAR_ROOT")

    ray.init()

    # Launch a ray actor to search for the log chunks that match the query.
    search = ArchiveSearch.remote(args.zarroot, args.find)

    # Launch a ray actor to aggregate all of the matching chunks.
    agg = Aggregator.remote()

    maxreaders = args.nreaders
    tasks = []

    # Loop over the search results and do an parallel aggregation up to
    # maxreaders in parallel.
    while True:
        location = ray.get(search.next.remote())
        if location == None:
            break
        # For each hit, we create a task to read the chunk and filter
        # to only the events that match the search.
        id = agg.mix.remote(location, args.merge)
        tasks.append(id)
        if len(tasks) >= maxreaders:
            _, tasks = ray.wait(tasks)

    # wait for everything to finish
    while len(tasks) > 0:
            _, tasks = ray.wait(tasks)

    # grab the result from the aggregation
    res = ray.get(agg.result.remote())

    if not args.text:
        os.sys.stdout.buffer.write(res)
    else:
        print(textzng(res))
