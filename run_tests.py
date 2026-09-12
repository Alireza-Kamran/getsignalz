import glob, subprocess, sys
fails = []
for t in sorted(glob.glob("/root/trade/test_*.py")):
    p = subprocess.run([sys.executable, t], capture_output=True, text=True,
                       cwd="/root/trade", timeout=600)
    last = [l for l in (p.stdout + p.stderr).strip().splitlines() if l.strip()]
    tail = last[-1][:90] if last else "(no output)"
    flag = "ok " if p.returncode == 0 else "FAIL"
    if p.returncode != 0:
        fails.append(t)
    print(f"{flag} {t.split('/')[-1]:28s} {tail}")
print(f"\n{len(fails)} failing suite(s)")
for f in fails:
    print("  ", f)
