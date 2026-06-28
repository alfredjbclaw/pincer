# Deploying a loop (point Alfred at a project)

One command registers a project; the driver (LaunchAgent, hourly) runs it autonomously.

    # maintain a repo: fix open issues, ship bugfixes, PR features, every 6h
    python3 tools/deploy.py add --name myproj --mode fix \
        --repo owner/name --workdir /abs/clone --target all-issues \
        --autonomy auto --schedule 6h

    # audit mode: find bugs, file them, fix them
    python3 tools/deploy.py add --name myproj --mode audit --repo owner/name --workdir /abs/clone --schedule 1d

    python3 tools/deploy.py run myproj      # fire once now (detached, Telegram)
    python3 tools/deploy.py list            # all deployed loops
    python3 tools/deploy.py disable myproj  # pause   (enable to resume)

Flags: --pr-only (never auto-merge), --max-coders N, --budget-window 80 --budget-week 80 (halt usage %).
Schedule: manual | Nh | Nm | Nd | always.   The repo needs a `.pincer.toml` test_command.
Driver: ai.openclaw.pincer-loop-driver (StartInterval 3600). Runs only enabled+due loops; budget-gated; single-runner lock; reports on signals.
