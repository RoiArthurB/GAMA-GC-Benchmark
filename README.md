# GAMA garbage-collector benchmark

`gc_bench.py` downloads a GAMA release (the `Linux_with_JDK` zip, i.e. the embedded Temurin users get) and
IBM Semeru 25 (OpenJ9), scales six library models up, runs them headless under ~35 GC configurations, and
writes `report.md` + `summary.csv`. Linux x86_64 and Python >= 3.9 only, no other dependency.

```sh
python3 gc_bench.py list                 # configurations and workloads
python3 gc_bench.py all --quick          # smoke test (~15 min): 10% of the cycles, 1 repetition
python3 gc_bench.py all                  # everything x 5 repetitions (~20 h on a 16-core desktop)
python3 gc_bench.py all --no-openj9 --configs 'g1*,zgc*,shen-gen*,parallel'   # a subset
python3 gc_bench.py report gc-bench-work/results other-machine/results     # merge several machines
```

- Stop everything else on the machine: each run uses all cores and up to 2x the base heap (default
  `min(8g, RAM/2)`); the report flags runs that started while the load average was above 2.
- Interrupted? Run the same command with the same `--batch` to resume.
- `--gama-zip <path|url>` benchmarks your own build instead of a release.

## On GitHub Actions

Copy this folder's content (`gc_bench.py` and `.github/`) to the root of a repository and start
*GAMA GC benchmark* from the Actions tab. Each job runs every configuration, interleaved, for one workload and
one repetition on its own VM (30 jobs by default), and the `report` job publishes the merged report in the run
summary. Use a public repository: `ubuntu-latest` then has 4 vCPU / 16 GB (private repositories get 2 vCPU,
which says little about a desktop and leaves no room for the big-heap configurations).

## What is measured

The probe injected into each model appends a timestamp per simulation cycle to a file, so startup and
compilation are excluded and the cycle-time distribution (p50/p99/max, the simulation's frame time) is
available. The JVM writes GC + safepoint logs (`-Xlog:gc*,safepoint`, or `-Xverbosegclog` on OpenJ9), which
give stop-the-world time, Shenandoah pacing and ZGC allocation stalls. CPU time and peak RSS come from
`wait4`. `epsilon` and `j9-nogc` never collect: they run until the heap is full, and the report compares
the cycles they reached with the same cycles of each collector, which estimates the total cost of GC.

Headless runs measure the simulation thread only. OpenGL/SWT displays add their own allocation and are
sensitive to pauses, so confirm the winners with a GUI run before changing `Gama.ini`.
