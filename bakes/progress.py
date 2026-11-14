#!/usr/bin/env python3
"""Progress and ETA for a running bake plan. Standard library only, so it runs on the GPU host as is.

    ATLAS_MAPS=<maps dir> python3 progress.py cook-strait.sh   # once
    watch -n 30 python3 progress.py cook-strait.sh

A job is done when its fantasy.pmtiles exists. The pace is the median gap between the last
few finished jobs, so a slow first chunk (cold model load) or an earlier failed run doesn't skew it.
"""

import os
import re
import statistics
import sys
import time

MAPS = os.environ.get("ATLAS_MAPS", "var/maps")  # the same ATLAS_MAPS the plan bakes into


def main(plan: str) -> None:
    jobs = re.findall(r"--out /data/out/atlas/(\S+)", open(plan).read())
    done = []
    for job in jobs:
        tiles = os.path.join(MAPS, job, "fantasy.pmtiles")
        if os.path.exists(tiles):
            done.append((os.path.getmtime(tiles), job))
    done.sort()
    total, n = len(jobs), len(done)
    print(f"{n}/{total} jobs done ({n / total:.0%})" if total else "no jobs in plan")
    if not done:
        return
    last_at, last_job = done[-1]
    print(f"last finished: {last_job}, {time.time() - last_at:.0f} s ago")
    gaps = [b - a for (a, _), (b, _) in zip(done, done[1:], strict=False)][-8:]
    if not gaps or n == total:
        return
    pace = statistics.median(gaps)
    left = (total - n) * pace
    finish = time.strftime("%H:%M", time.localtime(time.time() + left))
    print(f"pace: {pace:.0f} s/job, about {left / 60:.0f} min left (around {finish})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "cook-strait.sh")
