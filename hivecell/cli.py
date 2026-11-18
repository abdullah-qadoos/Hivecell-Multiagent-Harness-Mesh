import argparse
import asyncio
from pathlib import Path

from axonmesh import Mesh

from .orchestrator import Orchestrator, format_report, load_plan


def main(argv=None):
    p = argparse.ArgumentParser(prog="hivecell")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("plan", help="architect agent: spec -> plan YAML for you to review (the plan gate)")
    pl.add_argument("spec", help="spec file (markdown or text)")
    pl.add_argument("--repo", default=".", help="repo the architect may read (default: current folder)")
    pl.add_argument("--agents", default="agents", help="folder of agent rules files to choose from")
    pl.add_argument("--architect", help="architect rules file (default: <agents>/architect.yaml)")
    pl.add_argument("--out", default="plan.yaml")

    run = sub.add_parser("run", help="run a plan against a git repo")
    run.add_argument("plan", help="plan YAML file")
    run.add_argument("--repo", default=".", help="target git repo (default: current folder)")
    run.add_argument("--mode", choices=["plan", "auto", "ask"], default="ask",
                     help="plan: propose only; auto: any command (in Docker); ask: unlisted commands need approval")
    run.add_argument("--db", help="Axonmesh database (default: <repo>/.axonmesh/axon.db)")
    b = sub.add_parser("bench", help="benchmark: solo vs team vs team+review, scored by hidden acceptance tests")
    b.add_argument("--model", required=True, help="provider/model-id used by every agent")
    b.add_argument("--configs", default="solo,team,team-review")
    b.add_argument("--projects", help="comma-separated subset (default: all)")
    b.add_argument("--out", default="benchmarks/results.md")
    b.add_argument("--workdir", default=".hivecell-bench")
    args = p.parse_args(argv)

    if args.cmd == "bench":
        from .bench import main as bench

        return bench(args.model, args.configs.split(","), args.projects.split(",") if args.projects else None,
                     Path(args.out), Path(args.workdir).resolve())

    if args.cmd == "plan":
        from .architect import make_plan
        from .models import make_model
        from .rules import load_rules

        agents_dir = Path(args.agents)
        arch = load_rules(args.architect or agents_dir / "architect.yaml")
        files = sorted(agents_dir.glob("*.yaml"))
        out = Path(args.out)
        text, res = make_plan(Path(args.spec).read_text(encoding="utf-8"), Path(args.repo).resolve(), files, arch,
                              make_model(arch.model), out, lambda t, p: None)
        if not text:
            print(f"the architect did not produce a valid plan ({res.status})")
            return 1
        print(text)
        print(f"Plan written to {out} ({res.input_tokens + res.output_tokens} tokens). "
              f"Review or edit it, then: hivecell run {out} --repo <target>")
        return 0

    plan, rules = load_plan(args.plan)
    mesh = Mesh(Path(args.repo).resolve(), db_path=args.db)
    report = asyncio.run(Orchestrator(plan, rules, mesh, args.mode).run())
    print(format_report(report))
    return 0 if report.status in ("passed", "proposed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
