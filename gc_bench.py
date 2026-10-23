#!/usr/bin/env python3
"""GAMA garbage-collector benchmark: installs everything, runs everything, writes a report.

Needs only Linux x86_64 and Python >= 3.9 (standard library only).

  python3 gc_bench.py all                # install + run every config x workload x 5 reps + report
  python3 gc_bench.py all --quick        # smoke test: 10% of the cycles, 1 rep
  python3 gc_bench.py install            # download GAMA (with its embedded Temurin) and IBM Semeru (OpenJ9)
  python3 gc_bench.py run --configs 'g1*,zgc' --workloads boids --reps 3
  python3 gc_bench.py report [results.jsonl | dir ...]
  python3 gc_bench.py list               # configs and workloads

What it measures, per run:
  * sim_s       wall time of the simulated cycles only (startup and compilation excluded), from a probe
                injected in each model that writes a timestamp every cycle; for -batch workloads (many
                simulations in one JVM, see SCENARIOS) it is the makespan of all of them
  * cyc_p50/p99/max  cycle-time distribution (the simulation equivalent of frame times)
  * stw_ms      stop-the-world time from -Xlog:safepoint (HotSpot) or verbose GC <exclusive> (OpenJ9)
  * pacing_ms / alloc_stall_ms  Shenandoah pacer delays / ZGC allocation stalls (stops that are not safepoints)
  * cpu_s, maxrss_mb  CPU time (concurrent collectors trade CPU for pauses) and peak resident memory
Runs are interleaved (rep -> workload -> shuffled configs) so machine drift spreads over all configs, and
ratios are computed against the baseline config of the same batch/rep, which makes results from different
machines (e.g. GitHub runners) comparable. Re-running with the same --work and --batch resumes.
"""
import argparse, fnmatch, gzip, hashlib, json, math, os, platform, random, re, shutil, socket, stat, statistics as st
import subprocess, sys, tarfile, time, urllib.request, zipfile
from pathlib import Path

GAMA_REPO = "gama-platform/gama"
SEMERU_REPO = "ibmruntimes/semeru25-binaries"
GiB = 1024 ** 3

# ------------------------------------------------------------------------------------------------ workloads
# Library models scaled up so the collector has real work; each run lasts ~60 s on a 16-core desktop.
# subs are literal replacements that must all match (the script stops if a model changed upstream).
WORKLOADS = {
	"boids": dict(src="Demos/Boids", model="models/Boids.gaml", exp="Basic", cycles=250, subs=[
		("int number_of_agents <- 50 min: 1 max: 500", "int number_of_agents <- 1200"),
		("int width_and_height_of_environment <- 1000", "int width_and_height_of_environment <- 2000")]),
	"particles": dict(src="Demos/Life", model="Particle Life.gaml", exp="ParticleLife", cycles=150, subs=[
		("int nb_particles <- 2000", "int nb_particles <- 1200")]),
	"predprey": dict(src="Tutorials/Predator Prey Tutorial", model="models/Model 10.gaml", exp="prey_predator",
		cycles=400, subs=[
		("nb_preys_init <- 200;", "nb_preys_init <- 8000;"),
		("nb_predators_init <- 20;", "nb_predators_init <- 400;"),
		("grid vegetation_cell width: 50 height: 50", "grid vegetation_cell width: 250 height: 250")]),
	"ants": dict(src="Demos/Ants (Foraging and Sorting)", model="models/Ant Foraging.gaml", exp="Classic",
		cycles=300, subs=[
		("int gridsize <- 100 min: 30", "int gridsize <- 400 min: 30"),
		("int ants_number <- 200 min: 1", "int ants_number <- 5000 min: 1")]),
	"traffic": dict(src="Demos/Traffic", model="models/Traffic and Pollution.gaml", exp="traffic", cycles=400,
		subs=[("create people number: 1000", "create people number: 8000")]),
	"flu": dict(src="Tutorials/Luneray flu", model="models/model6.gaml", exp="main", cycles=150, subs=[
		("int nb_people <- 2147", "int nb_people <- 5000")]),
}
# Scenarios: every model runs as one simulation per JVM (the interactive / GUI-like case) and as a parallel batch:
# `parallel` simulations at once (default: every logical core) and `waves` simulations in a row on each worker, so
# later simulations start after a model reload while the previous ones are garbage. Batch simulations run
# `cycles` x `cycle_factor` cycles each.
SCENARIOS = {"": dict(batch=False), "-batch": dict(batch=True, cycle_factor=0.4)}
NOMINAL_RUN_S = {False: 75, True: 180}  # per run including JVM/GAMA startup, used only for the time estimate

# Appends one line per cycle to a file per simulation (named after its seed): 'write' is silent when the shipped
# Gama.ini disables logging, and a file survives a run that dies (zero-GC references). Timestamps are milliseconds
# since {base}, the same clock for every simulation of a run. Runs are sequential, so files are reused across runs.
PROBE = ("\n\treflex __bench_probe { save (\"BENCH_T \" + cycle + \" \" + int(gama.machine_time - {base})) "
		 "to: \"{path}-\" + int(seed) + \".txt\" format: \"text\" rewrite: false; }\n")


def workload_names():
	return [m + sfx for sfx in SCENARIOS for m in WORKLOADS]


def scenario_of(wl):
	for sfx, sc in SCENARIOS.items():
		if sfx and wl.endswith(sfx):
			return wl[:-len(sfx)], sc
	return wl, SCENARIOS[""]

# ------------------------------------------------------------------------------------------------ configs
# heap: "base" | "half" | "double" | "nogc" (sizes resolved from RAM at run time, see heap_sizes()).
# Flags may use {heap} (this config's -Xmx), {base}, {quarter} (of base) and {xms4} (min(4g, heap)) placeholders, in MB.
# Current tuning of gama.headless.product (heap flags removed; -Xms kept, capped to the heap).
G1_HEADLESS_INI = ("-XX:+UseG1GC -Xms{xms4}m -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200 "
	"-XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC -XX:+AlwaysPreTouch -XX:G1HeapWastePercent=5 "
	"-XX:G1MixedGCCountTarget=4 -XX:InitiatingHeapOccupancyPercent=15 -XX:G1MixedGCLiveThresholdPercent=90 "
	"-XX:G1RSetUpdatingPauseTimePercent=5 -XX:SurvivorRatio=32 -XX:+PerfDisableSharedMem -XX:MaxTenuringThreshold=1 "
	"-XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 -XX:G1HeapRegionSize=8M -XX:G1ReservePercent=20 "
	"-XX:+UseStringDeduplication -XX:+UseAdaptiveSizePolicy -XX:+OptimizeStringConcat")
CONFIGS = [
	# name, jvm, family, flags, heap, partial (expected to run out of memory: zero-GC reference)
	("serial", "hotspot", "Serial", "-XX:+UseSerialGC", "base", False),
	("parallel", "hotspot", "Parallel", "-XX:+UseParallelGC", "base", False),
	("parallel-coh", "hotspot", "Parallel", "-XX:+UseParallelGC -XX:+UseCompactObjectHeaders", "base", False),
	("g1", "hotspot", "G1", "-XX:+UseG1GC", "base", False),
	("g1-ini", "hotspot", "G1", "-XX:+UseG1GC -Xms{xms4}m -XX:+UnlockExperimentalVMOptions "
		"-XX:+UseAdaptiveSizePolicy -XX:+OptimizeStringConcat", "base", False),  # what Gama.ini ships
	("g1-headless-ini", "hotspot", "G1", G1_HEADLESS_INI, "base", False),
	("g1-p50", "hotspot", "G1", "-XX:+UseG1GC -XX:MaxGCPauseMillis=50", "base", False),
	("g1-p500", "hotspot", "G1", "-XX:+UseG1GC -XX:MaxGCPauseMillis=500", "base", False),
	("g1-fixed", "hotspot", "G1", "-XX:+UseG1GC -Xms{base}m -XX:+AlwaysPreTouch", "base", False),
	("g1-coh", "hotspot", "G1", "-XX:+UseG1GC -XX:+UseCompactObjectHeaders", "base", False),
	("zgc", "hotspot", "ZGC", "-XX:+UseZGC", "base", False),
	("zgc-soft", "hotspot", "ZGC", "-XX:+UseZGC -XX:SoftMaxHeapSize={quarter}m", "base", False),
	("zgc-coh", "hotspot", "ZGC", "-XX:+UseZGC -XX:+UseCompactObjectHeaders", "base", False),
	("zgc-conc2", "hotspot", "ZGC", "-XX:+UseZGC -XX:ConcGCThreads=2", "base", False),
	("shen", "hotspot", "Shenandoah", "-XX:+UseShenandoahGC", "base", False),
	("shen-compact", "hotspot", "Shenandoah", "-XX:+UseShenandoahGC -XX:ShenandoahGCHeuristics=compact", "base", False),
	("shen-gen", "hotspot", "Shenandoah", "-XX:+UseShenandoahGC -XX:ShenandoahGCMode=generational", "base", False),
	("shen-gen-coh", "hotspot", "Shenandoah", "-XX:+UseShenandoahGC -XX:ShenandoahGCMode=generational "
		"-XX:+UseCompactObjectHeaders", "base", False),
	("shen-passive", "hotspot", "Shenandoah", "-XX:+UnlockDiagnosticVMOptions -XX:+UseShenandoahGC "
		"-XX:ShenandoahGCMode=passive", "base", False),
	("g1-half", "hotspot", "G1", "-XX:+UseG1GC", "half", False),
	("g1-double", "hotspot", "G1", "-XX:+UseG1GC", "double", False),
	("parallel-half", "hotspot", "Parallel", "-XX:+UseParallelGC", "half", False),
	("parallel-double", "hotspot", "Parallel", "-XX:+UseParallelGC", "double", False),
	("zgc-half", "hotspot", "ZGC", "-XX:+UseZGC", "half", False),
	("zgc-double", "hotspot", "ZGC", "-XX:+UseZGC", "double", False),
	("shen-gen-half", "hotspot", "Shenandoah", "-XX:+UseShenandoahGC -XX:ShenandoahGCMode=generational", "half", False),
	("shen-gen-double", "hotspot", "Shenandoah", "-XX:+UseShenandoahGC -XX:ShenandoahGCMode=generational", "double", False),
	("epsilon", "hotspot", "Epsilon", "-XX:+UnlockExperimentalVMOptions -XX:+UseEpsilonGC -Xms{heap}m -XX:+AlwaysPreTouch", "nogc", True),
	("j9-gencon", "openj9", "OpenJ9", "-Xgcpolicy:gencon", "base", False),
	("j9-gencon-cs", "openj9", "OpenJ9", "-Xgcpolicy:gencon -Xgc:concurrentScavenge", "base", False),
	("j9-balanced", "openj9", "OpenJ9", "-Xgcpolicy:balanced", "base", False),
	("j9-optthruput", "openj9", "OpenJ9", "-Xgcpolicy:optthruput", "base", False),
	("j9-optavgpause", "openj9", "OpenJ9", "-Xgcpolicy:optavgpause", "base", False),
	("j9-metronome", "openj9", "OpenJ9", "-Xgcpolicy:metronome", "base", False),
	("j9-nogc", "openj9", "OpenJ9", "-Xgcpolicy:nogc", "nogc", True),
]
CFG = {c[0]: dict(name=c[0], jvm=c[1], family=c[2], flags=c[3], heap=c[4], partial=c[5]) for c in CONFIGS}
BASELINE = "g1"


def log(*a):
	print(*a, file=sys.stderr, flush=True)


# ------------------------------------------------------------------------------------------------ install
def gh_api(path):
	req = urllib.request.Request("https://api.github.com/" + path,
								 headers={"Accept": "application/vnd.github+json", "User-Agent": "gama-gc-bench"})
	tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
	if tok:
		req.add_header("Authorization", "Bearer " + tok)
	with urllib.request.urlopen(req, timeout=60) as r:
		return json.load(r)


def download(url, dest):
	dest = Path(dest)
	if dest.exists():
		return dest
	log(f"  downloading {url}")
	tmp = dest.with_suffix(dest.suffix + ".part")
	req = urllib.request.Request(url, headers={"User-Agent": "gama-gc-bench"})
	with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
		shutil.copyfileobj(r, f, 1 << 20)
	tmp.rename(dest)
	return dest


def sha256(path):
	h = hashlib.sha256()
	with open(path, "rb") as f:
		for b in iter(lambda: f.read(1 << 20), b""):
			h.update(b)
	return h.hexdigest()


def inside(root, p):
	root, p = os.path.realpath(root), os.path.realpath(p)
	return p == root or p.startswith(root + os.sep)


def extract_zip(zpath, dest):
	"""zipfile does not restore permissions: apply the unix mode stored in external_attr."""
	with zipfile.ZipFile(zpath) as z:
		for info in z.infolist():
			target = os.path.join(dest, info.filename)
			if not inside(dest, target):
				raise SystemExit(f"unsafe path in archive: {info.filename}")
			mode = info.external_attr >> 16
			if info.is_dir():
				os.makedirs(target, exist_ok=True)
				continue
			os.makedirs(os.path.dirname(target), exist_ok=True)
			if stat.S_ISLNK(mode):
				link = z.read(info).decode()
				if not os.path.isabs(link) and inside(dest, os.path.join(os.path.dirname(target), link)):
					os.symlink(link, target)
				continue
			with z.open(info) as src, open(target, "wb") as out:
				shutil.copyfileobj(src, out, 1 << 20)
			if mode & 0o777:
				os.chmod(target, mode & 0o777)


def extract_tar(tpath, dest):
	with tarfile.open(tpath) as t:
		if hasattr(tarfile, "data_filter"):
			t.extractall(dest, filter="data")
		else:
			for m in t.getmembers():
				if not inside(dest, os.path.join(dest, m.name)) or (m.issym() and os.path.isabs(m.linkname)):
					raise SystemExit(f"unsafe path in archive: {m.name}")
			t.extractall(dest)


def find_dir(root, predicate, depth=3):
	for dirpath, dirnames, _ in os.walk(root):
		if predicate(Path(dirpath)):
			return Path(dirpath)
		if dirpath[len(str(root)):].count(os.sep) >= depth:
			dirnames[:] = []
	return None


def install_gama(work, args):
	gdir = work / "gama"
	if (gdir / ".installed").exists():
		return json.loads((gdir / ".installed").read_text())
	dl = work / "downloads"; dl.mkdir(parents=True, exist_ok=True)
	info = {}
	if args.gama_zip:
		src = args.gama_zip
		zpath = download(src, dl / os.path.basename(src)) if re.match(r"https?://", src) else Path(src)
		info.update(source=src)
		if args.gama_sha256 and sha256(zpath) != args.gama_sha256.lower():
			raise SystemExit("GAMA zip sha256 mismatch")
	else:
		if args.gama_release == "latest":
			rel = next(r for r in gh_api(f"repos/{GAMA_REPO}/releases?per_page=30")
					   if any(re.search(r"Linux_with_JDK.*\.zip$", a["name"]) for a in r["assets"]))
		else:
			rel = gh_api(f"repos/{GAMA_REPO}/releases/tags/{args.gama_release}")
		asset = next(a for a in rel["assets"] if re.search(r"Linux_with_JDK.*\.zip$", a["name"]))
		zpath = download(asset["browser_download_url"], dl / asset["name"])
		info.update(source=asset["browser_download_url"], release=rel["tag_name"])
		chk = next((a for a in rel["assets"] if a["name"] == "checksum.txt"), None)
		if chk:
			txt = download(chk["browser_download_url"], dl / f"checksum-{rel['tag_name']}.txt").read_text()
			want = [l.split()[0] for l in txt.splitlines() if l.endswith(asset["name"]) and len(l.split()[0]) == 64]
			if want and sha256(zpath) != want[0]:
				raise SystemExit(f"checksum mismatch for {zpath}")
			info["sha256_verified"] = bool(want)
	log(f"  extracting {zpath}")
	shutil.rmtree(gdir, ignore_errors=True); gdir.mkdir(parents=True)
	extract_zip(zpath, gdir)
	root = find_dir(gdir, lambda p: (p / "Gama.ini").exists() and (p / "headless").is_dir())
	if not root:
		raise SystemExit("no Gama.ini + headless/ found in the GAMA archive")
	info["root"] = str(root)
	(gdir / ".installed").write_text(json.dumps(info))
	return info


def install_semeru(work, args):
	sdir = work / "semeru"
	if (sdir / ".installed").exists():
		return json.loads((sdir / ".installed").read_text())
	dl = work / "downloads"; dl.mkdir(parents=True, exist_ok=True)
	if args.semeru_release == "latest":
		rel = next(r for r in gh_api(f"repos/{SEMERU_REPO}/releases?per_page=30") if not r["prerelease"])
	else:
		rel = gh_api(f"repos/{SEMERU_REPO}/releases/tags/{args.semeru_release}")
	asset = next(a for a in rel["assets"] if re.fullmatch(r"ibm-semeru-open-jdk_x64_linux_.*\.tar\.gz", a["name"]))
	tpath = download(asset["browser_download_url"], dl / asset["name"])
	chk = next((a for a in rel["assets"] if a["name"] == asset["name"] + ".sha256.txt"), None)
	if chk:
		want = download(chk["browser_download_url"], dl / chk["name"]).read_text().split()[0]
		if sha256(tpath) != want:
			raise SystemExit(f"checksum mismatch for {tpath}")
	log(f"  extracting {tpath}")
	shutil.rmtree(sdir, ignore_errors=True); sdir.mkdir(parents=True)
	extract_tar(tpath, sdir)
	home = find_dir(sdir, lambda p: (p / "bin" / "java").exists())
	info = dict(release=rel["tag_name"], java_home=str(home))
	(sdir / ".installed").write_text(json.dumps(info))
	return info


def prepare_models(work, gama_root, scale, parallel, waves):
	"""Extract the library models, apply the workload scaling and inject the probe."""
	lib = work / "library"
	if not (lib / ".extracted").exists():
		shutil.rmtree(lib, ignore_errors=True)
		jar = next((gama_root / "plugins").glob("gama.library_*.jar"), None)
		if jar is None:
			raise SystemExit("gama.library_*.jar not found in the GAMA plugins")
		with zipfile.ZipFile(jar) as z:
			for n in z.namelist():
				if n.startswith("models/") and not n.endswith("/") and inside(lib, lib / n):
					(lib / n).parent.mkdir(parents=True, exist_ok=True)
					(lib / n).write_bytes(z.read(n))
		(lib / ".extracted").write_text("")
	mdir, xdir = work / "models", work / "xml"
	shutil.rmtree(mdir, ignore_errors=True); shutil.rmtree(xdir, ignore_errors=True)
	xdir.mkdir(parents=True)
	(work / "probe").mkdir(exist_ok=True)
	base = float(int(time.time() * 1000))  # probe clock origin; GAML int is 32-bit, so stay well under 24 days
	plans = {}
	for name, w in WORKLOADS.items():
		shutil.copytree(lib / "models" / w["src"], mdir / name)
		f = mdir / name / w["model"]
		code = f.read_text(encoding="utf-8")
		for old, new in w["subs"]:
			if old not in code:
				raise SystemExit(f"{name}: '{old}' not found in {w['model']} (model changed upstream?)")
			code = code.replace(old, new)
		probe = PROBE.replace("{path}", str(work / "probe" / name)).replace("{base}", repr(base))
		code, n = re.subn(r"^global[^{\n]*\{", lambda m: m.group(0) + probe, code, count=1, flags=re.M)
		if n != 1:
			raise SystemExit(f"{name}: no global block found for the probe")
		f.write_text(code, encoding="utf-8")
		for sfx, sc in SCENARIOS.items():
			par, wav = (parallel, waves) if sc["batch"] else (1, 1)
			cyc = max(10, int(round(w["cycles"] * scale * sc.get("cycle_factor", 1.0))))
			seeds = list(range(1, par * wav + 1)) if sc["batch"] else [42]
			sims = "".join(f"""  <Simulation id="{i}" sourcePath="{f}" finalStep="{cyc + 2}" experiment="{w['exp']}" seed="{sd}">
    <Parameters/>
    <Outputs/>
  </Simulation>
""" for i, sd in enumerate(seeds))
			(xdir / f"{name}{sfx}.xml").write_text(
				f'<?xml version="1.0" encoding="UTF-8"?>\n<Experiment_plan>\n{sims}</Experiment_plan>\n', encoding="utf-8")
			plans[name + sfx] = dict(model=name, target=cyc, parallel=par, waves=wav, seeds=seeds, batch=sc["batch"])
	return plans


# ------------------------------------------------------------------------------------------------ run
def meminfo(key):
	for l in open("/proc/meminfo"):
		if l.startswith(key + ":"):
			return int(l.split()[1]) * 1024
	return 16 * GiB


def mem_total():
	return meminfo("MemTotal")


def heap_sizes(arg, cap=8 * GiB):
	ram = mem_total()
	base = parse_size(arg) if arg != "auto" else min(cap, int(ram * 0.5))
	return dict(base=base, half=base // 2, double=min(2 * base, int(ram * 0.75)),
				# zero-GC references fill their whole heap: never more than what is free right now
				nogc=min(24 * GiB, int(ram * 0.6), int(meminfo("MemAvailable") * 0.8)))


def parse_size(s):
	m = re.fullmatch(r"(\d+)([gGmM])", s)
	if not m:
		raise SystemExit(f"bad size {s} (use e.g. 8g or 6144m)")
	return int(m.group(1)) * (GiB if m.group(2) in "gG" else 1024 ** 2)


def ini_jvm_args(gama_root, jvm):
	"""Non-GC JVM args of the shipped Gama.ini: heap and -XX flags are removed so only the config's apply."""
	lines = (gama_root / "Gama.ini").read_text().splitlines()
	toks = []
	for l in lines[lines.index("-vmargs") + 1:]:
		toks += l.split()
	out = [t for t in toks if not re.match(r"-(Xm[sx]|XX:|server$)", t)]
	if jvm == "openj9":
		# OpenJ9 rejects some HotSpot-only options (filled after testing against Semeru 25).
		out = [t for t in out if not t.startswith(tuple(OPENJ9_UNSUPPORTED))]
	return out + ["-Djava.awt.headless=true"]


OPENJ9_UNSUPPORTED = []


def config_cmd(cfg, heaps, gama_root, java, rundir):
	heap = heaps[cfg["heap"]]
	mb = lambda b: b // 1024 ** 2
	flags = cfg["flags"].format(heap=mb(heap), base=mb(heaps["base"]), quarter=mb(heaps["base"]) // 4,
								xms4=min(4096, mb(heap))).split()
	cmd = [java, f"-Xmx{mb(heap)}m"] + flags + ["-XX:+ExitOnOutOfMemoryError"]
	if cfg["jvm"] == "hotspot":
		cmd.append(f"-Xlog:gc*,safepoint:file={rundir}/gc.log:uptime,level,tags:filecount=0")
	else:
		cmd.append(f"-Xverbosegclog:{rundir}/gc.log")
	return cmd + ini_jvm_args(gama_root, cfg["jvm"]), mb(heap)


def preflight(cfgs, heaps, gama_root, javas, work):
	"""Drop configs this JVM refuses (e.g. Shenandoah missing from a build) instead of failing 6 runs each."""
	ok = []
	for c in cfgs:
		cmd, _ = config_cmd(CFG[c], heaps, gama_root, javas[CFG[c]["jvm"]], work)
		cmd = [x for x in cmd if not x.startswith(("-Xlog", "-Xverbosegclog"))] + ["-version"]
		cmd = [x for x in cmd if not x.startswith("-Xms") and x != "-XX:+AlwaysPreTouch"]
		p = subprocess.run(cmd, capture_output=True, text=True)
		if p.returncode == 0 and "Ignoring option" not in p.stderr:
			ok.append(c)
		else:
			log(f"  ! config {c} rejected by the JVM, skipped: {(p.stderr.strip().splitlines() or ['?'])[-1][:200]}")
	return ok


def pct(xs, p):
	xs = sorted(xs)
	return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else 0


def parse_run(rundir, plan):
	"""sim_s is the makespan: first cycle of the first simulation to last cycle of the last one."""
	target, warm = plan["target"], max(1, plan["target"] // 10)
	r = dict(target=target, sims=len(plan["seeds"]), parallel=plan["parallel"], waves=plan["waves"])
	per_sim, first, last = {}, [], []
	for sd in plan["seeds"]:
		f = rundir / "probe" / f"{plan['model']}-{sd}.txt"
		t = {}
		for c, ms in re.findall(r"BENCH_T (\d+) (-?\d+)", f.read_text(errors="replace") if f.exists() else ""):
			t[int(c)] = int(ms)
		cyc = [t[c + 1] - t[c] for c in range(min(target, max(t, default=0))) if c in t and c + 1 in t]
		per_sim[sd] = cyc
		if cyc:
			first.append(t[0]); last.append(t[len(cyc)])
	steady = [d for cyc in per_sim.values() for d in (cyc[warm:] or cyc)]
	done = [len(c) for c in per_sim.values()]
	r.update(cycles_done=sum(done), sims_done=sum(1 for n in done if n >= target),
			 complete=all(n >= target for n in done),
			 cyc=per_sim[plan["seeds"][0]] if len(per_sim) == 1 else [])  # per-cycle list kept for single runs only
	r["target"] = target * len(plan["seeds"])
	if steady:
		r.update(sim_s=(max(last) - min(first)) / 1000, cyc_p50=st.median(steady), cyc_p95=pct(steady, 95),
				 cyc_p99=pct(steady, 99), cyc_max=max(steady), cyc_mean=st.mean(steady))
	if plan["waves"] > 1:  # seeds are queued in order: seed i runs in wave (i-1) // parallel
		wave = lambda k: [d for sd, cyc in per_sim.items() if (sd - 1) // plan["parallel"] == k for d in cyc[warm:]]
		w0, wn = wave(0), wave(plan["waves"] - 1)
		if w0 and wn:
			r["wave_drift"] = st.mean(wn) / st.mean(w0)
	g = (rundir / "gc.log").read_text(errors="replace") if (rundir / "gc.log").exists() else ""
	if "<verbosegc" in g:  # OpenJ9: every stop-the-world section is an <exclusive-start/end> pair
		sp = [float(x) for x in re.findall(r"<exclusive-end[^>]*durationms=\"([\d.]+)\"", g)]
		r.update(gc_cycles=len(re.findall(r"<cycle-start", g)), pacing_ms=0.0, alloc_stall=0, alloc_stall_ms=0.0)
	else:
		sp = [int(x) / 1e6 for x in re.findall(r"Safepoint .*Total: (\d+) ns", g)]
		r["gc_cycles"] = len(set(re.findall(r"GC\((\d+)\)", g)))
		stalls = [float(x) for x in re.findall(r"Allocation Stall \([^)]*\) (\d+\.\d+)ms", g)]
		r.update(alloc_stall=len(stalls), alloc_stall_ms=sum(stalls))
		m = re.search(r"\] Pacing\s+=\s+([\d.]+) s", g)
		r["pacing_ms"] = float(m.group(1)) * 1000 if m else 0.0
		r["degenerated"] = len(re.findall(r"Pause Degenerated", g))
		r["full_gc"] = len(re.findall(r"Pause Full", g))
	r.update(stw_ms=sum(sp), stw_n=len(sp), stw_max=max(sp, default=0), stw_p99=pct(sp, 99))
	# GAMA headless exits with 0 even when the simulation dies on a runtime error: count the Java exceptions.
	out = (rundir / "stdout.log").read_text(errors="replace") if (rundir / "stdout.log").exists() else ""
	r["errors"] = len(re.findall(r"^(?:Caused by: )?[a-z][\w.$]*(?:Exception|Error)\b", out, re.M))
	return r


def cpu_busy(seconds=1.0):
	"""Fraction of the machine's CPU used by other processes right now (the 1-min load average still
	carries the previous run)."""
	def snap():
		v = list(map(int, open("/proc/stat").readline().split()[1:]))
		return sum(v), v[3] + v[4]
	t0, i0 = snap(); time.sleep(seconds); t1, i1 = snap()
	return 1 - (i1 - i0) / max(1, t1 - t0)


def run_one(cfg, wl, rep, plan, heaps, gama_root, javas, work, batch, timeout):
	rundir = work / "runs" / batch / wl / f"{cfg['name']}-r{rep}"
	shutil.rmtree(rundir, ignore_errors=True); rundir.mkdir(parents=True)
	cmd, heap_mb = config_cmd(cfg, heaps, gama_root, javas[cfg["jvm"]], rundir)
	shutil.copytree(gama_root / "headless" / "configuration", rundir / "configuration")
	(rundir / "ws").mkdir()
	launcher = next((gama_root / "plugins").glob("org.eclipse.equinox.launcher_*.jar"))
	cmd += ["-cp", str(launcher), "org.eclipse.equinox.launcher.Main", "-configuration", str(rundir / "configuration"),
			"-application", "gama.headless.product", "-data", str(rundir / "ws"), "-hpc", str(plan["parallel"]),
			str(work / "xml" / f"{wl}.xml"), str(rundir / "out")]
	(rundir / "cmd.txt").write_text("\n".join(cmd))
	for f in (work / "probe").glob(f"{plan['model']}-*.txt"):
		f.unlink()
	env = dict(os.environ, LC_ALL="C", LANG="C")
	load_before = os.getloadavg()[0]
	busy_before = cpu_busy()
	t0 = time.time()
	with open(rundir / "stdout.log", "w") as fo:
		p = subprocess.Popen(cmd, stdout=fo, stderr=subprocess.STDOUT, env=env, start_new_session=True)
		timed_out = False
		while True:
			pid, status, ru = os.wait4(p.pid, os.WNOHANG)
			if pid:
				break
			if time.time() - t0 > timeout:
				timed_out = True
				os.killpg(p.pid, 9)
				pid, status, ru = os.wait4(p.pid, 0)
				break
			time.sleep(0.25)
		p.returncode = os.waitstatus_to_exitcode(status)
	r = dict(batch=batch, host=socket.gethostname(), config=cfg["name"], family=cfg["family"], jvm=cfg["jvm"],
			 partial=cfg["partial"], workload=wl, rep=rep, heap_mb=heap_mb, exit=p.returncode, timed_out=timed_out,
			 wall_s=time.time() - t0, cpu_s=ru.ru_utime + ru.ru_stime, cpu_user=ru.ru_utime, cpu_sys=ru.ru_stime,
			 maxrss_mb=ru.ru_maxrss / 1024, load_before=load_before, busy_before=busy_before,
			 load_after=os.getloadavg()[0])
	(rundir / "probe").mkdir()
	for f in (work / "probe").glob(f"{plan['model']}-*.txt"):
		shutil.move(str(f), rundir / "probe" / f.name)
	r.update(parse_run(rundir, plan))
	for d in ("configuration", "ws", "out", "p2"):
		shutil.rmtree(rundir / d, ignore_errors=True)
	(rundir / "artifacts.xml").unlink(missing_ok=True)
	for f in ("gc.log", "stdout.log"):  # keep the logs, compressed
		if (rundir / f).exists():
			with open(rundir / f, "rb") as src, gzip.open(rundir / (f + ".gz"), "wb") as dst:
				shutil.copyfileobj(src, dst, 1 << 20)
			(rundir / f).unlink()
	return r


def select(names, pattern):
	if pattern in ("all", ""):
		return list(names)
	out = []
	for p in pattern.split(","):
		m = [n for n in names if fnmatch.fnmatch(n, p.strip())]
		if not m:
			raise SystemExit(f"'{p}' matches nothing; see `gc_bench.py list`")
		out += [n for n in m if n not in out]
	return out


def host_info(javas):
	cpu = next((l.split(":", 1)[1].strip() for l in open("/proc/cpuinfo") if l.startswith("model name")), "?")
	thp = Path("/sys/kernel/mm/transparent_hugepage/enabled")
	info = dict(host=socket.gethostname(), cpu=cpu, cores=os.cpu_count(), ram_gb=round(mem_total() / GiB, 1),
				kernel=platform.release(), thp=thp.read_text().strip() if thp.exists() else "?")
	for k, j in javas.items():
		info[f"java_{k}"] = subprocess.run([j, "-version"], capture_output=True, text=True).stderr.strip().splitlines()
	return info


def cmd_run(args, work):
	gama = install_gama(work, args)
	gama_root = Path(gama["root"])
	javas = {"hotspot": str(gama_root / "jdk" / "bin" / "java")}
	names = select([c[0] for c in CONFIGS], args.configs)
	if args.no_openj9:
		names = [n for n in names if CFG[n]["jvm"] != "openj9"]
	if any(CFG[n]["jvm"] == "openj9" for n in names):
		javas["openj9"] = str(Path(install_semeru(work, args)["java_home"]) / "bin" / "java")
	wls = select(workload_names(), args.workloads)
	parallel = args.parallel or os.cpu_count()
	plans = prepare_models(work, gama_root, args.scale, parallel, args.waves)
	heaps = {False: heap_sizes(args.heap), True: heap_sizes(args.heap_batch, cap=32 * GiB)}
	if heaps[False]["double"] <= heaps[False]["base"] * 1.2:
		names = [n for n in names if CFG[n]["heap"] != "double"]
		log("  not enough RAM for the 'double' heap configs, skipped")
	names = preflight(names, heaps[any(plans[w]["batch"] for w in wls)], gama_root, javas, work)
	# Zero-GC references fill their heap within seconds when many simulations run at once: single scenario only.
	cfgs_for = lambda wl: [n for n in names if not (plans[wl]["batch"] and CFG[n]["partial"])]
	batch = args.batch or f"{socket.gethostname()}-{time.strftime('%Y%m%d-%H%M%S')}"
	res = Path(args.results or work / "results" / f"{batch}.jsonl")
	res.parent.mkdir(parents=True, exist_ok=True)
	(res.parent / f"{batch}.host.json").write_text(json.dumps(host_info(javas), indent=1))
	done = set()
	if res.exists():
		for l in res.read_text().splitlines():
			r = json.loads(l)
			done.add((r["workload"], r["config"], r["rep"]))
	plan = [(rep, wl, c) for rep in range(args.rep_offset, args.rep_offset + args.reps) for wl in wls
			for c in random.Random(f"{batch}-{rep}-{wl}").sample(cfgs_for(wl), len(cfgs_for(wl))) if (wl, c, rep) not in done]
	est = sum(NOMINAL_RUN_S[plans[wl]["batch"]] for _, wl, _ in plan) * max(args.scale, 0.15) / 3600
	log(f"GAMA {gama.get('release', gama.get('source'))}; heap base={heaps[False]['base'] // 2**20}m "
		f"(batch scenario {heaps[True]['base'] // 2**20}m, {parallel} parallel x {args.waves} waves); "
		f"{len(names)} configs x {len(wls)} workloads x {args.reps} reps -> {len(plan)} runs to do "
		f"(~{est:.1f} h on a 16-core desktop); results: {res}")
	for i, (rep, wl, c) in enumerate(plan, 1):
		pl = plans[wl]
		r = run_one(CFG[c], wl, rep, pl, heaps[pl["batch"]], gama_root, javas, work, batch, args.timeout)
		with open(res, "a") as f:
			f.write(json.dumps(r) + "\n")
		state = ("partial" if CFG[c]["partial"] else "ok" if r["complete"] and not r["errors"]
				 else f"FAILED exit={r['exit']} errors={r['errors']}")
		if r["busy_before"] > 0.10:
			state += f"  (other processes used {r['busy_before']:.0%} of the CPU just before this run)"
		log(f"[{i}/{len(plan)}] {wl:15s} {c:16s} sim={r.get('sim_s', 0):7.1f}s p99={r.get('cyc_p99', 0):6.0f}ms "
			f"stw={r['stw_ms']:7.0f}ms cpu={r['cpu_s']:6.0f}s rss={r['maxrss_mb']:6.0f}MB "
			f"cycles={r['cycles_done']}/{r['target']} {state}")
	return res


# ------------------------------------------------------------------------------------------------ report
def gmean(xs):
	xs = [x for x in xs if x and x > 0]
	return math.exp(sum(map(math.log, xs)) / len(xs)) if xs else float("nan")


def load_results(paths):
	rows = []
	for p in map(Path, paths):
		files = sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]
		for f in files:
			rows += [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
	return rows


def cmd_report(paths, out_dir, baseline):
	rows = load_results(paths)
	if not rows:
		raise SystemExit("no results found")
	full = [r for r in rows if not r["partial"]]
	ok = [r for r in full if r["complete"] and not r.get("errors")]
	failed = [r for r in full if r not in ok]
	# Ratios against the baseline run of the same batch/workload/rep (same machine, same round).
	base = {(r["batch"], r["workload"], r["rep"]): r for r in ok if r["config"] == baseline}
	for r in ok:
		b = base.get((r["batch"], r["workload"], r["rep"]))
		for k in ("sim_s", "cyc_p99", "maxrss_mb", "cpu_s"):
			r[f"rel_{k}"] = r[k] / b[k] if b and b.get(k) else None
	wls = [w for w in workload_names() if any(r["workload"] == w for r in rows)]
	cfgs = sorted({r["config"] for r in ok}, key=lambda c: [x[0] for x in CONFIGS].index(c) if c in CFG else 999)
	grp = lambda w, c: [r for r in ok if r["workload"] == w and r["config"] == c]
	med = lambda rs, k: st.median([r[k] for r in rs if r.get(k) is not None]) if any(
		r.get(k) is not None for r in rs) else float("nan")
	L = [f"# GAMA garbage-collector benchmark", ""]
	hosts = {}
	for p in map(Path, paths):
		for f in (sorted(p.rglob("*.host.json")) if p.is_dir() else sorted(p.parent.glob("*.host.json"))):
			h = json.loads(f.read_text())
			hosts[(h["cpu"], h["cores"], h["ram_gb"])] = h
	for h in hosts.values():
		L.append(f"- Host: {h['cpu']}, {h['cores']} threads, {h['ram_gb']} GB RAM, kernel {h['kernel']}, THP {h['thp']}")
		for k in [k for k in h if k.startswith("java_")]:
			L.append(f"  - {k[5:]}: {' / '.join(h[k][:2])}")
	heaps = sorted({(r['config'], r['heap_mb']) for r in rows if r['config'] == baseline})
	L += [f"- {len(rows)} runs, {len({r['batch'] for r in rows})} batch(es); baseline `{baseline}`"
		  f"{' (heap ' + str(heaps[0][1]) + ' MB)' if heaps else ''}; ratios are per batch/rep then median.",
		  "- `sim time` covers the simulated cycles only; `p99 cycle` is the 99th percentile of cycle durations after "
		  "a 10% warm-up (the simulation's frame time); `STW` is safepoint time (HotSpot) or exclusive-access time "
		  "(OpenJ9); `pacing` is Shenandoah's per-thread throttling summed over threads (not a pause).",
		  "- OpenJ9 configs also change the JIT compiler, so their gap to HotSpot is JIT + GC, not GC alone; compare "
		  "OpenJ9 policies with each other, and HotSpot collectors with each other.", ""]
	L += ["- `-batch` workloads run many simulations in one JVM: `parallel` at once and `waves` in a row per worker "
		  "(later ones start after a model reload). Their `sim time` is the makespan of all simulations, their cycle "
		  "percentiles cover every simulation, and `wave drift` is the mean cycle time of the last wave divided by "
		  "the first (above 1 = later simulations ran slower).", ""]
	# One ranking per scenario: a collector can win the interactive case and lose the batch one
	for title, in_sc in (("single simulation per JVM (interactive, GUI-like)", lambda w: not scenario_of(w)[1]["batch"]),
						 ("parallel batch with model reloads", lambda w: scenario_of(w)[1]["batch"])):
		sw = [w for w in wls if in_sc(w)]
		if not sw:
			continue
		L += [f"## Ranking - {title} (geometric mean over workloads, relative to baseline; lower is better)", "",
			  "| config | family | sim time | p99 cycle | CPU | RSS | workloads | failures |", "|---|---|---|---|---|---|---|---|"]
		rank = []
		for c in cfgs:
			per = {k: [med(grp(w, c), f"rel_{k}") for w in sw if grp(w, c)] for k in ("sim_s", "cyc_p99", "cpu_s", "maxrss_mb")}
			nf = sum(1 for r in failed if r["config"] == c and r["workload"] in sw)
			if per["sim_s"] or nf:
				rank.append((gmean(per["sim_s"]), c, per, nf))
		for g, c, per, nf in sorted(rank, key=lambda x: (math.isnan(x[0]), x[0])):
			L.append(f"| `{c}` | {CFG.get(c, {}).get('family', '?')} | x{g:.3f} | x{gmean(per['cyc_p99']):.3f} | "
					 f"x{gmean(per['cpu_s']):.2f} | x{gmean(per['maxrss_mb']):.2f} | {len(per['sim_s'])} | {nf or ''} |")
		L.append("")
	# Per workload
	for w in wls:
		L += ["", f"## {w}", "",
			  "| config | n | sim s | vs base | spread | p50 ms | p99 ms | max ms | STW ms | STW max | pacing ms | CPU s | RSS MB | wave drift |",
			  "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
		for c in sorted([c for c in cfgs if grp(w, c)], key=lambda c: med(grp(w, c), "sim_s")):
			rs = grp(w, c)
			sims = [r["sim_s"] for r in rs]
			spread = (max(sims) - min(sims)) / st.median(sims) * 100
			rel = med(rs, "rel_sim_s")
			L.append(f"| `{c}` | {len(rs)} | {med(rs, 'sim_s'):.1f} | {(rel - 1) * 100:+.1f}% | {spread:.1f}% | "
					 f"{med(rs, 'cyc_p50'):.0f} | {med(rs, 'cyc_p99'):.0f} | {med(rs, 'cyc_max'):.0f} | "
					 f"{med(rs, 'stw_ms'):.0f} | {med(rs, 'stw_max'):.1f} | {med(rs, 'pacing_ms'):.0f} | "
					 f"{med(rs, 'cpu_s'):.0f} | {med(rs, 'maxrss_mb'):.0f} | "
					 f"{'' if math.isnan(med(rs, 'wave_drift')) else format(med(rs, 'wave_drift'), '.2f')} |")
	# Zero-GC references: compare the cycles they completed with the same cycles of every other run.
	refs = [r for r in rows if r["partial"] and r["cycles_done"] > max(1, r["target"] // 10) + 5]
	if refs:
		L += ["", "## GC cost vs. zero-GC reference (Epsilon / OpenJ9 nogc)", "",
			  "Each reference runs without any collector until the heap is full; the table compares the cycles it "
			  "completed (after warm-up) with the same cycles of each config of the same JVM, same batch/rep: "
			  "`+12%` means that config spends 12% more time than with no GC at all on that stretch. Negative values "
			  "are real: a collector that recycles a small young generation keeps it in the CPU caches, while a "
			  "no-GC heap walks through fresh memory for every allocation (Epsilon pre-touches its heap, so page "
			  "faults are excluded; OpenJ9 nogc cannot).", "",
			  "| workload | reference | cycles | " + " | ".join(f"`{c}`" for c in cfgs) + " |",
			  "|---|---|---|" + "---|" * len(cfgs)]
		for w in wls:
			for refname in sorted({r["config"] for r in refs if r["workload"] == w}):
				cells, ncyc = [], []
				refjvm = CFG.get(refname, {}).get("jvm")
				for c in cfgs:
					vals = []
					if CFG.get(c, {}).get("jvm") != refjvm:
						cells.append("")
						continue
					for ref in [r for r in refs if r["workload"] == w and r["config"] == refname]:
						lo, hi = max(1, ref["target"] // 10), ref["cycles_done"]
						for r in ok:
							if (r["batch"], r["workload"], r["rep"], r["config"]) == (ref["batch"], w, ref["rep"], c):
								vals.append(sum(r["cyc"][lo:hi]) / max(1, sum(ref["cyc"][lo:hi])))
								ncyc.append(hi - lo)
					cells.append(f"{(st.median(vals) - 1) * 100:+.0f}%" if vals else "")
				L.append(f"| {w} | `{refname}` | {st.median(ncyc) if ncyc else 0:.0f} | " + " | ".join(cells) + " |")
	if failed:
		L += ["", "## Failed or incomplete runs", "",
			  "Excluded from the tables above. `exceptions` counts Java exceptions printed by GAMA: headless keeps "
			  "exit code 0 when a simulation dies on a runtime error.", "",
			  "| workload | config | rep | batch | exit | timed out | cycles | exceptions |", "|---|---|---|---|---|---|---|---|"]
		for r in failed:
			L.append(f"| {r['workload']} | `{r['config']}` | {r['rep']} | {r['batch']} | {r['exit']} | "
					 f"{r['timed_out']} | {r['cycles_done']}/{r['target']} | {r.get('errors', '?')} |")
	busy = [r for r in rows if r.get("busy_before", 0) > 0.10]
	if busy:
		L += ["", f"> {len(busy)} runs started while other processes used more than 10% of the CPU "
				  f"(worst: {max(r['busy_before'] for r in busy):.0%}); compare their spread with the others."]
	out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
	(out_dir / "report.md").write_text("\n".join(L) + "\n")
	keys = ["batch", "host", "workload", "config", "family", "jvm", "rep", "heap_mb", "exit", "complete", "cycles_done",
			"target", "errors", "sim_s", "rel_sim_s", "cyc_p50", "cyc_p95", "cyc_p99", "cyc_max", "stw_ms", "stw_n", "stw_max",
			"pacing_ms", "alloc_stall_ms", "gc_cycles", "cpu_s", "maxrss_mb", "sims", "parallel", "waves", "wave_drift", "wall_s", "busy_before", "load_before", "load_after"]
	with open(out_dir / "summary.csv", "w") as f:
		f.write(",".join(keys) + "\n")
		for r in rows:
			f.write(",".join("" if r.get(k) is None else str(r.get(k)) for k in keys) + "\n")
	log(f"report: {out_dir / 'report.md'}  csv: {out_dir / 'summary.csv'}")
	return out_dir / "report.md"


# ------------------------------------------------------------------------------------------------ main
def main():
	ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	ap.add_argument("command", choices=["all", "install", "run", "report", "list"])
	ap.add_argument("paths", nargs="*", help="report: results files or directories (default: <work>/results)")
	ap.add_argument("--work", default="gc-bench-work", help="working directory (downloads, runs, results)")
	ap.add_argument("--gama-release", default="latest", help="GAMA release tag (default: latest with a Linux JDK zip)")
	ap.add_argument("--gama-zip", help="use this GAMA 'Linux_with_JDK' zip (path or URL) instead of a release")
	ap.add_argument("--gama-sha256", help="expected sha256 of --gama-zip")
	ap.add_argument("--semeru-release", default="latest", help="IBM Semeru 25 tag for the OpenJ9 track")
	ap.add_argument("--no-openj9", action="store_true", help="skip the OpenJ9 configs")
	ap.add_argument("--configs", default="all", help="comma-separated names or globs (e.g. 'g1*,zgc')")
	ap.add_argument("--workloads", default="all", help="comma-separated workload names or globs")
	ap.add_argument("--reps", type=int, default=5)
	ap.add_argument("--rep-offset", type=int, default=0, help="first rep index (to split reps over machines)")
	ap.add_argument("--scale", type=float, default=1.0, help="multiplier on the number of simulated cycles")
	ap.add_argument("--heap", default="auto", help="base -Xmx, single scenario (default: min(8g, RAM/2))")
	ap.add_argument("--heap-batch", default="auto", help="base -Xmx, -batch scenario (default: min(32g, RAM/2))")
	ap.add_argument("--parallel", type=int, default=0, help="-batch: simulations at once (default: logical cores)")
	ap.add_argument("--waves", type=int, default=3, help="-batch: simulations in a row per worker (model reloads)")
	ap.add_argument("--batch", help="batch id (default: host-date); reusing one resumes it")
	ap.add_argument("--results", help="results .jsonl (default: <work>/results/<batch>.jsonl)")
	ap.add_argument("--timeout", type=int, default=2400, help="seconds before a run is killed")
	ap.add_argument("--baseline", default=BASELINE)
	ap.add_argument("--quick", action="store_true", help="smoke test: --scale 0.1 --reps 1")
	args = ap.parse_args()
	if args.quick:
		args.scale, args.reps = 0.1, 1
	if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
		raise SystemExit("Linux x86_64 only")
	work = Path(args.work).resolve()
	work.mkdir(parents=True, exist_ok=True)
	if args.command == "list":
		h = heap_sizes(args.heap)
		print("configs:")
		for c in CONFIGS:
			print(f"  {c[0]:18s} {c[1]:8s} heap={c[4]:6s}({h[c[4]] // 2**20}m) {c[3]}{'  [zero-GC reference]' if c[5] else ''}")
		print("workloads (each also as <name>-batch: --parallel simulations at once x --waves in a row, "
			  f"{SCENARIOS['-batch']['cycle_factor']:.0%} of the cycles each):")
		for n, w in WORKLOADS.items():
			print(f"  {n:10s} {w['src']}/{w['model']} [{w['exp']}] {w['cycles']} cycles")
	elif args.command == "install":
		log(install_gama(work, args))
		if not args.no_openj9:
			log(install_semeru(work, args))
	elif args.command == "run":
		cmd_run(args, work)
	elif args.command == "report":
		cmd_report(args.paths or [work / "results"], work, args.baseline)
	else:
		res = cmd_run(args, work)
		cmd_report([res], work, args.baseline)


if __name__ == "__main__":
	main()
