"""Pause across processes (Pacer): real subprocesses, offline (MockTransport), a short pause only in a test Policy."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path

import httpx
import pytest
from test_core import FakeTime

from kataster_mcp.envelope import KatasterError
from kataster_mcp.http import Http
from kataster_mcp.pacing import Pacer
from kataster_mcp.policy import PUBLIC_POLICY
from kataster_mcp.server import create_server

SRC = str(Path(__file__).parents[1] / "src")

# One worker process: waits for the go file, then makes `n` requests to `host`, logging start/end wall times.
WORKER = r'''
import asyncio, json, os, sys, time
import httpx
from kataster_mcp.http import Http
from kataster_mcp.pacing import Pacer
from kataster_mcp.policy import Policy

data, log, go, host, n, busy, pause = sys.argv[1:8]
n, busy, pause = int(n), float(busy), float(pause)

def handler(request):
    start = time.time()
    with open(log + ".started", "a") as f:
        f.write("x")
    time.sleep(busy)
    with open(log, "a") as f:
        f.write(json.dumps([os.getpid(), request.url.host, start, time.time()]) + "\n")
    return httpx.Response(200, json={})

async def main():
    policy = Policy(eskn_pause_s=pause, eskn_jitter_s=0.0, other_pause_s=pause)
    http = Http(policy, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    http.pacer = Pacer(os.path.join(data, "tempo"))
    if host == "hold":  # take the ESKN lock and never let go (the test kills this process)
        async with http.pacer.hold("kataster.skgeodesy.sk"):
            open(log + ".held", "w").close()
            await asyncio.sleep(60)
    open(log + ".ready", "w").close()
    while not os.path.exists(go):
        await asyncio.sleep(0.01)
    for i in range(n):
        await http.get(f"https://{host}/{i}")

asyncio.run(main())
'''


def spawn(tmp_path, name, host, n=1, busy=0.0, pause=0.5, go=None):
    script = tmp_path / "worker.py"
    script.write_text(WORKER, encoding="utf8")
    env = {**os.environ, "PYTHONPATH": SRC}
    log = tmp_path / f"{name}.log"
    go = go or tmp_path / "go"
    proc = subprocess.Popen([sys.executable, str(script), str(tmp_path / "data"), str(log), str(go), host,
                             str(n), str(busy), str(pause)], env=env, stdout=subprocess.PIPE, text=True)
    return proc, log


def wait_for(path: Path, timeout=20.0):
    end = time.time() + timeout
    while not path.exists():
        assert time.time() < end, f"{path.name} did not appear"
        time.sleep(0.01)


def rows(*logs):
    return sorted((json.loads(line) for log in logs for line in log.read_text().splitlines()), key=lambda r: r[2])


def test_two_processes_keep_the_pause_and_never_overlap(tmp_path):
    a, log_a = spawn(tmp_path, "a", "kataster.skgeodesy.sk", n=3, busy=0.1)
    b, log_b = spawn(tmp_path, "b", "kataster.skgeodesy.sk", n=3, busy=0.1)
    wait_for(tmp_path / "a.log.ready")
    wait_for(tmp_path / "b.log.ready")
    (tmp_path / "go").touch()
    assert a.wait(30) == 0 and b.wait(30) == 0
    r = rows(log_a, log_b)
    assert len(r) == 6 and len({x[0] for x in r}) == 2, r
    gaps = [nxt[2] - prev[3] for prev, nxt in pairwise(r)]
    assert min(gaps) >= 0.5 - 0.02, gaps  # pause after the end of the previous request, whoever sent it


def test_one_host_does_not_hold_up_another(tmp_path):
    slow, log_slow = spawn(tmp_path, "slow", "kataster.skgeodesy.sk", busy=3.0)
    (tmp_path / "go").touch()
    wait_for(tmp_path / "slow.log.started")  # the ESKN request is running and holds its lock
    t0 = time.time()
    fast, log_fast = spawn(tmp_path, "fast", "inspirews.skgeodesy.sk")
    assert fast.wait(30) == 0
    done = rows(log_fast)[0][3]
    assert not log_slow.exists() or rows(log_slow)[0][3] > done  # WFS finished while ESKN was still busy
    assert done - t0 < 2.5
    assert slow.wait(30) == 0


def test_killed_process_does_not_block_others(tmp_path):
    holder, _ = spawn(tmp_path, "holder", "hold")
    wait_for(tmp_path / "holder.log.held")
    holder.send_signal(signal.SIGKILL)
    holder.wait(10)
    t0 = time.time()
    other, log = spawn(tmp_path, "other", "kataster.skgeodesy.sk")
    (tmp_path / "go").touch()
    assert other.wait(30) == 0
    assert len(rows(log)) == 1 and time.time() - t0 < 10


def test_block_seen_by_another_process(tmp_path):
    """A CAPTCHA met by a server in one process stops a server in another process (other data dir) without a request."""
    script = tmp_path / "block.py"
    script.write_text(
        "import asyncio, pathlib, sys\nimport httpx\nfrom kataster_mcp.http import Http\n"
        "from kataster_mcp.policy import PUBLIC_POLICY\nfrom kataster_mcp.server import create_server\n"
        "h = lambda r: httpx.Response(200, text='<script src=\"https://www.google.com/recaptcha/api.js\"></script>',"
        " headers={'content-type': 'text/html'})\n"
        "s = create_server(PUBLIC_POLICY, data_dir=pathlib.Path(sys.argv[1]), limits_dir=pathlib.Path(sys.argv[2]),"
        " http=Http(PUBLIC_POLICY, client=httpx.AsyncClient(transport=httpx.MockTransport(h))))\n"
        "try:\n    asyncio.run(s.app_state.http.get('https://kataster.skgeodesy.sk/x'))\n"
        "except Exception as e:\n    print(e.kind)\n", encoding="utf8")
    calls = []
    ft = FakeTime()
    here = create_server(PUBLIC_POLICY, data_dir=tmp_path / "data", limits_dir=tmp_path / "limity", http=Http(
        PUBLIC_POLICY, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))),
        sleep=ft.sleep, clock=ft.clock))  # started before the block happens elsewhere
    out = subprocess.run([sys.executable, str(script), str(tmp_path / "data2"), str(tmp_path / "limity")], env={**os.environ, "PYTHONPATH": SRC},
                         capture_output=True, text=True, timeout=30, check=False).stdout
    assert "captcha_required" in out
    with pytest.raises(KatasterError) as e:
        asyncio.run(here.app_state.http.get("https://kataster.skgeodesy.sk/y"))
    assert e.value.kind == "blocked" and calls == []


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_clock_set_back_gives_the_whole_pause(tmp_path):
    other = Pacer(tmp_path, wall=lambda: 10_000.0)
    async with other.hold("kataster.skgeodesy.sk"):
        pass
    ft = FakeTime()
    http = Http(PUBLIC_POLICY, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
                sleep=ft.sleep, clock=ft.clock)
    http.pacer = Pacer(tmp_path, wall=lambda: 9_000.0)  # this process's clock is behind the other's by 1000 s
    await http.get("https://kataster.skgeodesy.sk/a")
    assert len(ft.slept) == 1 and 3.0 <= ft.slept[0] <= 4.0  # the whole pause, not more, not less



@pytest.mark.anyio
async def test_own_requests_paced_by_monotonic_clock_only(tmp_path):
    ft = FakeTime()
    http = Http(PUBLIC_POLICY, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
                sleep=ft.sleep, clock=ft.clock)
    http.pacer = Pacer(tmp_path)
    for _ in range(3):
        await http.get("https://kataster.skgeodesy.sk/x")
    assert len(ft.slept) == 2 and all(3.0 <= s <= 4.0 for s in ft.slept)


def test_server_always_sets_a_pacer_per_host(tmp_path):
    http = Http(PUBLIC_POLICY)
    s = create_server(PUBLIC_POLICY, data_dir=tmp_path / "data", limits_dir=tmp_path / "limity", http=http)
    assert isinstance(s.app_state.http.pacer, Pacer) and s.app_state.http.pacer.dir == tmp_path / "limity" / "tempo"
    with pytest.raises(ValueError):
        s.app_state.http.pacer._conn("../x")

