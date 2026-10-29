"""Compare *all* MNA DC unknowns (node voltages and branch currents) with ngspice.

Run from project root:
    python tests/compare_spice.py --project . --cases tests/test_cases

Your custom netlist syntax is supported, with .op before .end.
"""

import argparse
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
OWN_VALUE = re.compile(rf"^\s*([\w.\-]+)\s*:\s*({NUMBER}|nan|[-+]?inf)\s*$", re.I | re.M)


def convert_netlist(source):
    """Return translated SPICE text and a list of (MNA label, ngspice expression)."""
    lines = source.splitlines()
    if not lines:
        raise ValueError("Empty netlist")

    output = [lines[0].strip() or "MNA test"]
    nodes = set()
    branches = []
    names = set()
    found_op = False

    for raw in lines[1:]:
        line = raw.strip()
        if not line or line.startswith(("*", "#")):
            continue
        if line.lower() == ".end":
            break
        if line.lower() == ".op":
            found_op = True
            continue
        if line.startswith("."):
            continue

        parts = line.split()
        name = parts[0]
        kind = name.upper()
        if kind in names:
            raise ValueError(f"Duplicate element name: {name}")
        names.add(kind)

        if kind.startswith(("VCVS", "VCCS")):
            if len(parts) != 6:
                raise ValueError(f"Expected 6 fields: {line}")
            _, n1, n2, cn1, cn2, gain = parts
            nodes.update((n1, n2, cn1, cn2))
            if kind.startswith("VCVS"):
                spice_name = "E" + name
                output.append(f"{spice_name} {n1} {n2} {cn1} {cn2} {gain}")
                branches.append((f"vcvs_{name[4:].lower()}_{float(gain):g}", f"{spice_name.lower()}#branch"))
            else:
                # Custom VCCS current direction: n2 -> n1
                output.append(f"G{name} {n2} {n1} {cn1} {cn2} {gain}")

        elif kind.startswith(("CCCS", "CCVS")):
            if len(parts) != 5:
                raise ValueError(f"Expected 5 fields: {line}")
            _, n1, n2, sense, gain = parts
            nodes.update((n1, n2))
            if kind.startswith("CCCS"):
                # Custom CCCS current direction: n2 -> n1
                output.append(f"F{name} {n2} {n1} {sense} {gain}")
            else:
                spice_name = "H" + name
                output.append(f"{spice_name} {n1} {n2} {sense} {gain}")
                branches.append((f"ccvs_{name[4:].lower()}_{float(gain):g}", f"{spice_name.lower()}#branch"))

        else:
            if len(parts) != 4:
                raise ValueError(f"Expected 4 fields: {line}")
            _, n1, n2, value = parts
            nodes.update((n1, n2))
            if kind.startswith("I"):
                # Custom independent-current-source direction: n2 -> n1
                output.append(f"{name} {n2} {n1} DC {value}")
            elif kind.startswith("V"):
                output.append(f"{name} {n1} {n2} DC {value}")
                branches.append((f"vs_{name[1:].lower()}_{float(value):g}", f"{name.lower()}#branch"))
            elif kind.startswith("L"):
                output.append(line)
                # ngspice exposes inductor current as @<inductor>[i].
                branches.append((f"l_{name[1:].lower()}_{float(value):g}", f"@{name.lower()}[i]"))
            elif kind.startswith(("R", "C")):
                output.append(line)
            else:
                raise ValueError(f"Unsupported element: {name}")

    if not found_op:
        raise ValueError("No .op directive before .end")

    nodes.discard("0")
    ordered_nodes = sorted(
        nodes,
        key=lambda n: (int(n[1:]) if re.fullmatch(r"n\d+", n, re.I) else math.inf, n.lower()),
    )
    measurements = [(n.lower(), f"v({n.lower()})") for n in ordered_nodes] + branches

    output.extend([".control", "op"])
    output.extend(f"print {spice_expr}" for _, spice_expr in measurements)
    output.extend(["quit", ".endc", ".end"])
    return "\n".join(output) + "\n", measurements


def run_own_solver(source, main_script, project):
    result = subprocess.run(
        [sys.executable, str(main_script)], cwd=project,
        input=source, text=True, capture_output=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError(f"Your solver failed:\n{result.stdout}\n{result.stderr}")
    values = {name.lower(): float(value) for name, value in OWN_VALUE.findall(result.stdout)}
    if not values:
        raise RuntimeError(f"No MNA unknowns found in your solver output:\n{result.stdout}")
    return values


def run_ngspice(netlist, ngspice_command, measurements):
    with tempfile.TemporaryDirectory() as temp:
        filename = Path(temp) / "reference.cir"
        logfile = Path(temp) / "reference.log"
        filename.write_text(netlist, encoding="utf-8")
        result = subprocess.run(
            [ngspice_command, "-b", "-o", str(logfile), str(filename)],
            text=True, capture_output=True, timeout=30,
        )
        log = logfile.read_text(encoding="utf-8", errors="replace") if logfile.exists() else ""
        transcript = "\n".join((log, result.stdout, result.stderr))
    if result.returncode:
        raise RuntimeError(f"ngspice failed (exit {result.returncode}):\n{transcript}")

    values = {}
    for own_name, expr in measurements:
        # Example print lines: v(n1) = 1.000000e+01; vin#branch = -5.0e-03
        pattern = re.compile(rf"(?im)^\s*{re.escape(expr)}\s*=\s*({NUMBER})\s*$")
        matches = pattern.findall(transcript)
        if matches:
            values[own_name] = float(matches[-1])
    missing = [(name, expr) for name, expr in measurements if name not in values]
    if missing:
        detail = ", ".join(f"{name} ({expr})" for name, expr in missing)
        raise RuntimeError(f"Missing ngspice measurements: {detail}\n\n{transcript}")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("."))
    parser.add_argument("--cases", type=Path, default=Path("tests/test_cases"))
    parser.add_argument("--ngspice", default="ngspice")
    parser.add_argument("--atol", type=float, default=1e-8)
    parser.add_argument("--rtol", type=float, default=1e-5)
    args = parser.parse_args()

    project = args.project.resolve()
    main_script = project / "src" / "main.py"
    if not main_script.is_file():
        parser.error(f"Missing {main_script}")
    if not shutil.which(args.ngspice) and not Path(args.ngspice).is_file():
        parser.error("ngspice not found; use --ngspice PATH")
    cases = sorted(args.cases.glob("*.cir")) + sorted(args.cases.glob("*.txt"))
    if not cases:
        parser.error(f"No test cases found in {args.cases}")

    failures = 0
    for path in cases:
        try:
            source = path.read_text(encoding="utf-8")
            translated, measurements = convert_netlist(source)
            own = run_own_solver(source, main_script, project)
            ref = run_ngspice(translated, args.ngspice, measurements)
            print(f"\n{path.name}")
            for name, expr in measurements:
                if name not in own:
                    raise RuntimeError(f"Missing MNA unknown '{name}' (SPICE expression: {expr})")
                a, b = own[name], ref[name]
                ok = math.isfinite(a) and math.isclose(a, b, rel_tol=args.rtol, abs_tol=args.atol)
                print(f"  {name:22} MNA={a: .9g}  ngspice={b: .9g}  {'PASS' if ok else 'FAIL'}")
                failures += not ok
            extras = set(own) - {name for name, _ in measurements}
            if extras:
                raise RuntimeError(f"Unexpected MNA unknowns: {', '.join(sorted(extras))}")
        except Exception as exc:
            failures += 1
            print(f"\n{path.name}: ERROR: {exc}")
    print(f"\n{'PASS' if failures == 0 else 'FAIL'} — {failures} discrepancies/errors")
    return int(failures != 0)


if __name__ == "__main__":
    raise SystemExit(main())
