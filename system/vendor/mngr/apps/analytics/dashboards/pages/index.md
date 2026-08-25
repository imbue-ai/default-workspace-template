---
title: Bare-metal boxes
---

Host metrics for the bare-metal slice boxes registered to one dev env
(default: `dev-josh-1`), read from the tier's OpenObserve telemetry parquet.
Data freshness depends on the last run of `extract_box_metrics.py`.

```sql servers
select
    host_name,
    plan_code,
    region,
    status,
    cpu_threads,
    ram_gb,
    disk_gb,
    slot_count,
    slice_count,
    leased_slice_count
from boxes.servers
order by host_name
```

```sql fleet_summary
select
    count(*) as box_count,
    sum(slot_count) as total_slots,
    sum(slice_count) as provisioned_slices,
    sum(leased_slice_count) as leased_slices
from boxes.servers
```

<BigValue data={fleet_summary} value=box_count title="Boxes" />
<BigValue data={fleet_summary} value=total_slots title="Slice slots" />
<BigValue data={fleet_summary} value=provisioned_slices title="Slices provisioned" />
<BigValue data={fleet_summary} value=leased_slices title="Slices leased" />

<DataTable data={servers} rows=10>
    <Column id=host_name title="Host" />
    <Column id=plan_code title="Plan" />
    <Column id=region title="Region" />
    <Column id=status title="Status" />
    <Column id=cpu_threads title="Threads" />
    <Column id=ram_gb title="RAM (GB)" />
    <Column id=disk_gb title="Disk (GB)" />
    <Column id=slot_count title="Slots" />
    <Column id=slice_count title="Slices" />
    <Column id=leased_slice_count title="Leased" />
</DataTable>

## CPU

```sql cpu_utilization
select bucket_at, host_name, busy_share, iowait_share
from boxes.cpu_utilization
order by bucket_at
```

<LineChart
    data={cpu_utilization}
    x=bucket_at
    y=busy_share
    series=host_name
    yFmt=pct1
    title="CPU utilization (5-minute average, all cores)"
    yMax=1
/>

```sql load_average
select bucket_at, host_name, load_1m, load_5m, load_15m
from boxes.load_average
order by bucket_at
```

<LineChart
    data={load_average}
    x=bucket_at
    y=load_5m
    series=host_name
    title="Load average (5m; see the servers table for each box's logical CPU count)"
/>

## Memory

```sql memory_in_use
select
    bucket_at,
    host_name,
    sum(bytes_used) filter (where state in ('used', 'slab_unreclaimable')) / 1e9 as used_gb,
    sum(bytes_used) filter (where state in ('cached', 'buffered', 'slab_reclaimable')) / 1e9 as cache_gb,
    sum(bytes_used) / 1e9 as total_gb
from boxes.memory_usage
group by bucket_at, host_name
order by bucket_at
```

<LineChart
    data={memory_in_use}
    x=bucket_at
    y={["used_gb", "cache_gb"]}
    series=host_name
    title="Memory in use (GB): used vs cache/buffers"
/>

## Slices (qemu processes)

Per-slice visibility comes from box-level qemu process metrics — there is no
collector inside the lima VMs.

```sql qemu_slices
select bucket_at, host_name, qemu_process_count, total_memory_bytes / 1e9 as qemu_memory_gb
from boxes.qemu_slices
order by bucket_at
```

<LineChart
    data={qemu_slices}
    x=bucket_at
    y=qemu_process_count
    series=host_name
    step=true
    title="Running qemu processes per box"
/>

<LineChart
    data={qemu_slices}
    x=bucket_at
    y=qemu_memory_gb
    series=host_name
    title="Total qemu memory per box (GB)"
/>

## Disk

```sql filesystem_latest
select host_name, mountpoint, type, used_bytes / 1e9 as used_gb, free_bytes / 1e9 as free_gb, used_share
from boxes.filesystem_latest
order by host_name, mountpoint
```

<DataTable data={filesystem_latest}>
    <Column id=host_name title="Host" />
    <Column id=mountpoint title="Mount" />
    <Column id=type title="FS" />
    <Column id=used_gb title="Used (GB)" fmt=num1 />
    <Column id=free_gb title="Free (GB)" fmt=num1 />
    <Column id=used_share title="Used %" fmt=pct1 contentType=colorscale />
</DataTable>

```sql root_filesystem_history
select bucket_at, host_name, used_share
from boxes.filesystem_history
where mountpoint = '/'
order by bucket_at
```

<LineChart
    data={root_filesystem_history}
    x=bucket_at
    y=used_share
    series=host_name
    yFmt=pct1
    yMax=1
    title="Root filesystem usage"
/>

## Network

```sql network_throughput
select
    bucket_at,
    host_name || ' ' || direction as series_label,
    bytes_per_second / 1e3 as kb_per_second
from boxes.network_throughput
order by bucket_at
```

<LineChart
    data={network_throughput}
    x=bucket_at
    y=kb_per_second
    series=series_label
    yFmt=num0
    title="Network throughput (KB/s, 5-minute average)"
/>
