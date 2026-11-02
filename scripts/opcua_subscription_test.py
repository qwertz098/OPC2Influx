"""
Compare OPC UA polling with subscriptions against a real server.

Phase 1 polls all nodes like the app does (chunked Read every interval).
Phase 2 subscribes to the same nodes and counts the change notifications.
Both phases report the client CPU time, so the saving can be read off directly.

Node list, either:
  --xlsx subscriptions.xlsx   export from the app (Subscriptions → Export);
                              use --server to pick one server's rows
  --browse "ns=4;s=|var|..."  collect variables below that node (default: Objects)

Example (Windows, from the project folder):
  .venv\\Scripts\\python scripts\\opcua_subscription_test.py opc.tcp://192.168.1.17:4840 --xlsx subscriptions.xlsx

Requires asyncua and openpyxl (both in requirements.txt).
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import logging
import time

from asyncua import Client, ua


def load_xlsx(path: str, server: str | None) -> list[str]:
    from openpyxl import load_workbook

    ws = load_workbook(path, read_only=True).active
    rows = ws.iter_rows(values_only=True)
    header = [str(h) for h in next(rows)]
    i_node, i_srv, i_en = header.index("Node ID"), header.index("Server"), header.index("Enabled")
    nodes = []
    for row in rows:
        if not row or not row[i_node] or row[i_en] != "Yes":
            continue
        if server and row[i_srv] != server:
            continue
        nodes.append(str(row[i_node]))
    return nodes


async def browse_variables(client: Client, root: str | None, limit: int) -> list[str]:
    start = client.get_node(root) if root else client.nodes.objects
    found: list[str] = []
    stack = [start]
    seen = set()
    while stack and len(found) < limit:
        node = stack.pop()
        for child in await node.get_children():
            key = child.nodeid.to_string()
            if key in seen or key.startswith("i="):  # skip the standard Server namespace
                continue
            seen.add(key)
            cls = await child.read_node_class()
            if cls == ua.NodeClass.Variable:
                found.append(key)
                if len(found) >= limit:
                    break
            elif cls in (ua.NodeClass.Object, ua.NodeClass.View):
                stack.append(child)
    return found


async def read_limit(client: Client, name: str):
    """Server capability (e.g. "OperationLimits_MaxNodesPerRead"); None if not reported."""
    try:
        node = client.get_node(getattr(ua.ObjectIds, f"Server_ServerCapabilities_{name}"))
        return await node.read_value()
    except Exception:
        return None


class Counter:
    def __init__(self) -> None:
        self.per_second: collections.Counter[int] = collections.Counter()
        self.per_node: collections.Counter[str] = collections.Counter()
        self.total = 0

    def datachange_notification(self, node, val, data) -> None:
        self.total += 1
        self.per_second[int(time.monotonic())] += 1
        self.per_node[node.nodeid.to_string()] += 1

    def status_change_notification(self, status) -> None:
        print(f"  ! subscription status change: {status}")


async def poll_phase(client: Client, nodes, interval: float, duration: float, chunk: int) -> float:
    cpu0, end = time.process_time(), time.monotonic() + duration
    cycles = 0
    while time.monotonic() < end:
        t0 = time.monotonic()
        for i in range(0, len(nodes), chunk):
            await client.read_values(nodes[i:i + chunk])
        cycles += 1
        await asyncio.sleep(max(0.0, interval - (time.monotonic() - t0)))
    cpu = time.process_time() - cpu0
    print(f"  {cycles} poll cycles, {cycles * len(nodes) / duration:.0f} values/s read")
    return cpu


async def subscription_phase(client: Client, nodes, interval: float, duration: float,
                             chunk: int, warmup: float) -> tuple[float, Counter, int]:
    counter = Counter()
    sub = await client.create_subscription(interval * 1000, counter)
    accepted, rejected = 0, collections.Counter()
    for i in range(0, len(nodes), chunk):
        results = await sub.subscribe_data_change(nodes[i:i + chunk], sampling_interval=interval * 1000)
        for r in results:
            if isinstance(r, ua.StatusCode):
                rejected[r.name] += 1
            else:
                accepted += 1
    print(f"  monitored items accepted: {accepted}, rejected: {sum(rejected.values())}"
          + (f" {dict(rejected)}" if rejected else ""))

    # The first notification per item is its initial value, not a change
    await asyncio.sleep(warmup)
    initial = counter.total
    cpu0, t0 = time.process_time(), int(time.monotonic())
    counter.per_node.clear()
    await asyncio.sleep(duration)
    cpu = time.process_time() - cpu0
    changes = counter.total - initial
    await sub.delete()
    print(f"  notifications during {warmup:.0f}s warm-up (initial values): {initial}")
    print(f"  changes in {duration:.0f}s: {changes} ({changes / duration:.1f}/s)")
    busiest = [n for s, n in sorted(counter.per_second.items()) if s >= t0][:-1]
    if busiest:
        print(f"  changes per second: min {min(busiest)}, max {max(busiest)}")
    return cpu, counter, accepted


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("endpoint", nargs="?", default="opc.tcp://192.168.1.17:4840")
    ap.add_argument("--xlsx", help="subscriptions export from the app")
    ap.add_argument("--server", help="only rows of this server (display name) from --xlsx")
    ap.add_argument("--browse", help="node id to browse for variables (default: Objects)")
    ap.add_argument("--max", type=int, default=1100, help="max nodes when browsing (default 1100)")
    ap.add_argument("--interval", type=float, default=1.0, help="poll/sampling interval in s (default 1)")
    ap.add_argument("--duration", type=float, default=60, help="seconds per phase (default 60)")
    ap.add_argument("--chunk", type=int, default=100, help="nodes per Read / CreateMonitoredItems call")
    ap.add_argument("--user")
    ap.add_argument("--password")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    client = Client(args.endpoint, timeout=10)
    if args.user:
        client.set_user(args.user)
        client.set_password(args.password or "")
    async with client:
        print(f"Connected to {args.endpoint}")
        # Limits the server advertises (0 or missing = not reported / unlimited)
        read_chunk = sub_chunk = args.chunk
        for name in ("OperationLimits_MaxNodesPerRead", "OperationLimits_MaxMonitoredItemsPerCall",
                     "MaxSubscriptions", "MaxSubscriptionsPerSession", "MaxMonitoredItems",
                     "MaxMonitoredItemsPerSubscription"):
            value = await read_limit(client, name)
            print(f"  {name.split('_')[-1]}: {value if value else 'not reported'}")
            if value and name.endswith("MaxNodesPerRead"):
                read_chunk = min(read_chunk, value)
            elif value and name.endswith("MaxMonitoredItemsPerCall"):
                sub_chunk = min(sub_chunk, value)

        if args.xlsx:
            ids = load_xlsx(args.xlsx, args.server)
        else:
            print("Browsing for variables…")
            ids = await browse_variables(client, args.browse, args.max)
        nodes = [client.get_node(i) for i in ids]
        print(f"Testing {len(nodes)} nodes, interval {args.interval}s, {args.duration:.0f}s per phase\n")
        if not nodes:
            return

        print("Phase 1: polling (like the app today)")
        cpu_poll = await poll_phase(client, nodes, args.interval, args.duration, read_chunk)
        print(f"  client CPU time: {cpu_poll:.2f}s\n")

        print("Phase 2: subscription")
        cpu_sub, counter, accepted = await subscription_phase(
            client, nodes, args.interval, args.duration, sub_chunk, warmup=max(5.0, 3 * args.interval))
        print(f"  client CPU time: {cpu_sub:.2f}s\n")

        changed = len(counter.per_node)
        print("Summary")
        print(f"  values handled per second: polling {len(nodes) / args.interval:.0f}, "
              f"subscription {sum(counter.per_node.values()) / args.duration:.1f}")
        print(f"  nodes that changed at least once: {changed} of {accepted}")
        if cpu_poll > 0:
            print(f"  client CPU: subscription uses {cpu_sub / cpu_poll * 100:.0f}% of polling")
        top = counter.per_node.most_common(10)
        if top:
            print("  most frequently changing nodes:")
            for node_id, n in top:
                print(f"    {n / args.duration:5.1f}/s  {node_id}")


if __name__ == "__main__":
    asyncio.run(main())
