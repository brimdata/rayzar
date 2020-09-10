# rayzar

This repo contains some very rough and early thinking about how
[zq](https://github.com/brimsec/zq),
[zar](https://github.com/brimsec/zq/tree/master/cmd/zar),
and
[zng](https://github.com/brimsec/zq/blob/master/zng/docs/spec.md),
can be used with
[Ray](https://github.com/ray-project/ray) python jobs to perform
highly scalable search and analytics.
Hence, the repo name "rayzar" (pronounced "razor").

> NOTE: The toy code here is meant merely as a vehicle for discussion.
> It is not at all efficient and currently execs zq and zar processes
> from python.  There are different ways to approach a production-quality
> integration and this document and example code is meant to provide
> a starting point for exploring the options.

## data model

The zq data model for analytics and search comprises a set of stored
data files in the zng format organized in a directory hierarchy that partitions
the data in an arbitrary fashion.
Typically, when it comes to logs arranged by time, the partitions are organized by
year-month-day, or year-month-day-hour, etc.  We presume here that while the data
is stored in chunks that are partitioned by time, data might
be sliced and diced different ways across an arbitrary number of files
within any given partition.

The zng data is indexed with [micro-indexes](https://github.com/brimsec/zq/tree/master/cmd/zar).
zar creates and manages the indexes. For the discussion here, we presume indexes
have been created and we will use zar to search the indexes and stream data to
Ray actors.  In these examples, zng logs and zar indexes are stored in the file
system of the local host and we run Ray in this prototype configuration all
on localhost.

## Ray

Ray provides a distributed computation model for python where you can create
stateful actors and run stateless tasks that all coordinate to carry out
distributed computation.  Actors store state as
immutable objects that other actors and tasks can access.  Actors and
tasks can launch other actors and tasks and the Ray scheduler and distributed
object manager coordinate activity across the cluster.

## setup

To try out the example is this repo, you first need to have the zq tools in your path.
If you haven't already done so, clone the repo and follow the instructions in the
[zq README](https://github.com/brimsec/zq)
to install the tools, which simply involves making sure you have Go installed etc,
and running
```
make install
```

Ray requires Python 3. If you do not already have Python 3 set up, a convenient way to do that is with pyenv (https://github.com/pyenv/pyenv) which you can install with brew.

To install Ray:
```
pip install ray
```

To get sample data you can clone the zq-sample-data repo, and make it available in a ZAR_ROOT with the following commands (or something similar):
```
mkdir /tmp/logs
export ZAR_ROOT=/tmp/logs
zq zq-sample-data/zng/*.gz | zar import -s 25MB -
```

TODO: what to do with this dataset in ray? I tried looking at a few of the records with:
```
zar zq "*" | zq -t - | less
```
But it looks like the example command below is expecting different data than zq-sample-data/zng. 

## run the example

To try out the example, run something like this:
```
./rayzardemo.py --find=":ip=192.168.0.51" -N=4 \
        --filter="192.168.0.51 | count() by id.orig_h,id.resp_h" \
        --merge="sum(count) as count by id.orig_h,id.resp_h" | zq -t -
```
Here, the `--find` argument specifies a `zar find` predicate,
the `--filter` argument provides a zql command that is applied to each file found,
and the `--merge` argument provides a zql command that combines the output of  
each filtered result into a running result.  

The degree of concurrency is controlled by the `-N` argument.

In the toy example here, a zar search is implemented as a ray actor that traverses
a zar archive enumerating the data files that match a provided search expression.
This is implemented by the
[ArchiveSearch class](https://github.com/brimsec/rayzar/blob/2b6efde844abc30eb60e3e700987d16de441febe/rayzardemo.py#L44).
The `next` method on this actor returns each subsequent file matching the search
at each call.  (This could be more efficiently streamed but currently all of the
results are read in a blocking manner then they are return one-by-one via the
`next` method.)

For each matching file, a
[Ray task](https://github.com/brimsec/rayzar/blob/2b6efde844abc30eb60e3e700987d16de441febe/rayzardemo.py#L30)
is run to read the file by inefficiently execing zq, applying the specified zql filter,
and returning the data return by zq in one shot.

The [Aggregator class](https://github.com/brimsec/rayzar/blob/2b6efde844abc30eb60e3e700987d16de441febe/rayzardemo.py#L63)
is a Ray actor that aggregates together each of the results that is read and
filtered through the zq reader.

Upon completion, the binary
zng output of the merge aggregation is sent to standard out and in the command above,
converted it to text zng via `zq -t`.

This is all incredibly inefficient because all
the data is passed through the plasma object store --- this is just meant as a first cut
here to start brainstorming.  A better approach would be to have a data reader
efficiently read the filtered results straight into memory that could then be
operated upon before being resulted into the plasma object store in the Arrow format.

## incremental deployment model

If you have an existing set of data accessible by Ray,
you could conceivably index this data with zar and add a search capability
to any jobs that traverse the stored data file by file using the zar-find actor
from this example.
Then, any existing code that processed batches of files using ray/zar would then
simply process the subset of files that matched the search.  And since the search
runs very fast by consulting index tables, the job would speed up very dramatically
for searches that materially narrow down the number of files that need to be scanned.

In a sense, this is like spark doing a pushdown of a filter predicate into
ELK or a SQL database when these sources are used as input to an analytics.
But in the scenario here, where the source of data is a bunch of raw files,
spark can't push down the filter and must read all the data in every file.

By using zar and attaching micro-indexes to each file,
a search can be combined with a large data traversal without the
need of a search system like ELK or a database indexing system like SQL.
In a sense, we are moving the search-index concept out of the database and/or
search silo and into the data-lake storage layer so that a data-lake analytics
job can exploit the micro-indexes to optimize its traversal of very large amounts
of data.  And rather than search indexes being some embedded data structure
in a monolithic system, they are exposed as simple zng files and become
first-class objects, exposed to the developer to use a building block
in whatever analytics and tooling they can dream up.

## decomposable aggregations

While Ray is incredibly powerful, it might prove to be overkill for many simpler
scenarios of data-lake analytics where you don't necessarily need machine-learning
models, GPU scheduling, parallel simulations, and so forth.

In particular, a so-called "decomposable aggregation" can be distributed and
aggregated very efficiently.  These aggregations have the property that
the overall result can be computed as a combination of partial results.

A "count" aggregation is an obvious example where if you partition
the data into two sets and count certain values in each set you can sum the
resulting counts to get the total count.  Sum, average, variance, set-union,
null, and so forth are all decomposable aggregations.  Large-scale joins
can also fit into this framework.

This concept isn't completely new.  Many existing systems
take advantage of the decomposable aggregations,
but when programming map-reduce jobs in a data lake,
you typically have to structure your code specifically to take
advantage of this property.  For example, in spark, you can use the
reduceByKey operator with an aggregation method instead of the standard
groupBy operator to cause partial aggregations to be computed at the
output stage of spark's map phase, a so called map-combine-reduce pattern.

So, let's talk about a hypothetical search query over some implied time range:
```
blah | sum(foo) by bar
```
This searches for all data that matches the pattern `blah` and
sums the values of field `foo` with respect to each value in the field `bar`.

As anyone knows who has spent time working on OLAP internals or systems like
spark or map-reduce, since sum is a decomposable aggregation, this can be sped up
by partitioning the time range across some number of processors and aggregating
the results across the processors as they complete.

If the values of bar comprise a low-cardinality set, then this would work fine
and you could choose any level of parallelism.  However, if `bar` has high cardinality,
then the workers could run out of memory.

Unfortunately, you don't know ahead of time the cardinality of the group-by condition.

This is why hadoop map-reduce then spark were created.  Hadoop dealt with this
memory problem by spilling data to disk and shuffling and merging data from disk
to produce the output.  Spark improved upon this by trying to keep everything
in memory and doing more intelligent inter-cluster shuffles to avoid round-trips
to disk if and when possible.

Both hadoop and spark implemented the fully general map-reduce model.
But what if we focused on zql-style queries leveraging the typical case of
decomposable aggregations and integrate into the design the use of search indexes
during query traversals?  And what if this were all exposed as simple
unix tools that can be experimented with from the command-line without
setting up complex clusters?

### version 1: a static model

The hadoopy approach for all this is to just spill to disk.  Any aggregation
(or sort, or other grouping operation)
in a zql flowgraph will have a table, organized by a set of group-by or
sort keys (one or more of primary, second, etc key).  To deal with
memory limits, whenever this table hits a configured size limit (related
to the amount of memory dedicated to the worker),
it is simply spilled to disk by writing the entries to disk
in a key-sorted order.  At end-of-stream, the tables are all opened
and read in parallel and any entry that is common across the streams is
aggregated by the aggregation operator and the results are streamed
to the downstream entity.

This approach can be parallelized across N nodes where the time range of the
traversal is divided amongst the different nodes and the final step merges
the streamed results from all of the nodes.

That will work fine for many use cases.
For many aggregations, the group-by table will stay small when the cardinality
of the key space is low.
If you have a feel for how much memory you need ahead of time, you could
launch N jobs in parallel so a high-cardinality group-by table
gets split out efficiently.  In this case, the key space isn't disjoint
but it might not matter much if the sub-cardinality of each key is small
(meaning there is little advantage to a shuffle)
so in general this could work well depending on the underlying data
characteristic.  And if the memory limit is hit here, the table is spilled
to disk as above.

### version 2: a dynamic model

There are two key problems with the static approach.  First, you don't generally
know how to choose N so it's unreasonable to ask the user to specify this.
And second, partitioning the work by time means the key space is replicated
over each node making the tables inefficient and larger than they need to be.

A solution to these problems is, firstly, to fork a new worker only after the
aggregation table gets too big (thus providing scale-out memory along with
scale-out throughput) and, secondly, to implement a shuffle to
route the table rows to the worker that owns the row.  Ownership of rows
is handled like spark and map-reduce using a hash partition.  The
number of dynamic nodes is limited either because the cluster
manager (e.g., kubernetes) indicates no more resources are available
or a configured scale-out limit is hit.  Either way, when there are no
more available resources, workers then spill to disk.

In this approach, we begin with N=M workers for throughput parallelism then
dynamically add workers (increasing N) to provide additional memory scale-out
and throughput parallelism.  The traversal range is divided among the M workers
in some way (i.e., evenly divided, partitioned with a block/M stride, etc).
For now, we assume these initial M workers do not partition the key space and
instead the result are merged at the end.  The key space could also be
partitioned across the initial workers with some small changes to the approach
outlined below.

When a worker hits a memory limit because its aggregation table gets to big,
it splits its job, say in 2 (though this fanout can be configured), or it resorts
to spilling if no resource is available for the split.  When it splits,
half of the key space and half of the input partition goes to the new child.

Once split, the worker initiates a _continuous shuffle_, where it pushes
rows that it does not own to the owner of those rows.  At any point in time,
every worker knows the other N-1 workers in the job, how the keys are partitioned
across the workers, and its individual position in the set of workers.  Thus,
each worker knows where to send rows that it does not own.

To optimize the continuous shuffle, each worker keeps N-1 _peer tables_ to batch
the streaming updates.  As records arrive in this table, they can be aggregated
before transmission potentially reducing the overhead.  In general,
larger peer tables results in lower overhead.  A configuration
parameter controls the ratio of the size of the peer table storage to the
primary table.  Note that the peer tables do not need to all be the same size and
can shared a dynamic size to better optimize non-uniform data patterns.

At end-of-stream, a worker flushes its peer tables and streams back its
table (perhaps mixing in any spills) to its parent in response to the
REST call that invoked that worker.  This process of
writing results may block if the parent is not yet done.  If so, when the
parent is done and ready, it will read the results from all of its children,
and propagate the merged results in sorted order to its parent and so forth.

The state required for reach worker to carry all this out is simply the list
of endpoints that exist and any point in time along with the key space
partitioning strategy.  Because of the properties of decomposable aggregations,
this state merely needs to be reliably sent to all participating workers and
the partial updates will all flow to their respective owners in an
eventually-consistent manner.

The implementation of all of this turns out to be remarkably simple given the
zq software architecture.  There is no need for a centralized controller as in
spark and hadoop to schedule and manage sequential processing stages.  It goes
without saying that the data flowing between workers are simply zng streams.
Also, the interactions between workers are straightforward REST calls not unlike
the REST calls that the brim app issues to zqd.

Everything here is elegantly asynchronous and event driven.
