"""Tests for bin/fleet. Run: python3 -m unittest discover -s tests

Everything runs against a temporary config, a temporary LaunchAgents directory
and a fake launchctl; the real launchd is never touched.
"""
import importlib.machinery
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLEET = os.path.join(ROOT, "bin", "fleet")
FAKE = os.path.join(ROOT, "tests", "fake_launchctl")


def load_module():
    loader = importlib.machinery.SourceFileLoader("fleet_mod", FLEET)
    spec = importlib.util.spec_from_loader("fleet_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fleet = load_module()


def svc(**kw):
    s = {"name": "web", "label": "local.web", "run": {"argv": ["/bin/echo", "hi"]}}
    s.update(kw)
    return s


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-test-")
        self.cfg = os.path.join(self.tmp, "config", "services.json")
        self.agents = os.path.join(self.tmp, "LaunchAgents")
        self.state = os.path.join(self.tmp, "launchctl.json")
        os.makedirs(self.agents)
        self.env = dict(os.environ, FLEET_CONFIG=self.cfg, FLEET_AGENTS_DIR=self.agents,
                        FLEET_LAUNCHCTL=FAKE, FAKE_LAUNCHCTL_STATE=self.state,
                        FLEET_SETTLE="0", HOME=self.tmp)
        self._saved = {k: os.environ.get(k) for k in ("FLEET_CONFIG", "FLEET_AGENTS_DIR", "FLEET_LAUNCHCTL",
                                                      "FAKE_LAUNCHCTL_STATE")}
        for k in self._saved:
            os.environ[k] = self.env[k]

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp)

    def write_cfg(self, services):
        os.makedirs(os.path.dirname(self.cfg), exist_ok=True)
        with open(self.cfg, "w") as f:
            json.dump({"version": 1, "services": services}, f)

    def read_cfg(self):
        with open(self.cfg) as f:
            return json.load(f)

    def run_fleet(self, *args, check=True):
        p = subprocess.run([sys.executable, FLEET] + list(args), env=self.env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                            timeout=15)
        if check and p.returncode != 0:
            self.fail("fleet %s exited %d\nstdout: %s\nstderr: %s" % (args, p.returncode, p.stdout, p.stderr))
        return p

    def lstate(self):
        with open(self.state) as f:
            return json.load(f)


class PlistGeneration(unittest.TestCase):
    ENV = {"PATH": "/usr/bin:/bin", "HOME": "/example/home", "USER": "demo"}

    def test_fields(self):
        s = svc(log="/tmp/web.log", run={"argv": ["/bin/echo", "hi"], "cwd": "/srv",
                                         "env": {"PORT": "9"}, "keepAlive": False})
        d = plistlib.loads(fleet.render_plist(s, self.ENV))
        self.assertEqual(d["Label"], "local.web")
        self.assertEqual(d["ProgramArguments"], ["/bin/echo", "hi"])
        self.assertEqual(d["WorkingDirectory"], "/srv")
        self.assertEqual(d["EnvironmentVariables"],
                          {"PATH": "/usr/bin:/bin", "HOME": "/example/home", "USER": "demo", "LOGNAME": "demo", "PORT": "9"})
        self.assertIs(d["RunAtLoad"], True)
        self.assertIs(d["KeepAlive"], False)
        self.assertEqual(d["ProcessType"], "Background")
        self.assertEqual(d["StandardOutPath"], "/tmp/web.log")
        self.assertEqual(d["StandardErrorPath"], "/tmp/web.log")

    def test_defaults_keepalive_and_no_log(self):
        d = plistlib.loads(fleet.render_plist(svc(), self.ENV))
        self.assertIs(d["KeepAlive"], True)
        self.assertNotIn("StandardOutPath", d)
        self.assertNotIn("WorkingDirectory", d)

    def test_escaping(self):
        nasty = 'a&b <c> "d" \'e\' ]]>'
        s = svc(run={"argv": ["/bin/echo", nasty], "env": {"X": nasty}})
        xml = fleet.render_plist(s, self.ENV)
        self.assertNotIn(b"<c>", xml)
        self.assertIn(b"a&amp;b &lt;c&gt;", xml)
        d = plistlib.loads(xml)
        self.assertEqual(d["ProgramArguments"][1], nasty)
        self.assertEqual(d["EnvironmentVariables"]["X"], nasty)
        if shutil.which("plutil"):
            with tempfile.NamedTemporaryFile(suffix=".plist") as f:
                f.write(xml)
                f.flush()
                self.assertEqual(subprocess.call(["plutil", "-lint", "-s", f.name]), 0)

    def test_tilde_and_bare_program(self):
        s = svc(log="~/logs/w.log", run={"argv": ["sh", "~/x.sh", "a~b"], "cwd": "~/proj"})
        d = fleet.build_plist(s, self.ENV)
        home = os.path.expanduser("~")
        self.assertEqual(d["ProgramArguments"], ["/bin/sh", home + "/x.sh", "a~b"])
        self.assertEqual(d["WorkingDirectory"], home + "/proj")
        self.assertEqual(d["StandardOutPath"], home + "/logs/w.log")


class Validation(unittest.TestCase):
    def errs(self, services, **top):
        cfg = {"services": services}
        cfg.update(top)
        return "\n".join(fleet.validate(cfg))

    def test_valid(self):
        self.assertEqual(fleet.validate({"version": 1, "services": [
            svc(port=80, probe="/health", group="g", log="/x", url_from_log="t=(\\w+)"),
            {"name": "b", "label": "local.b", "adopt": {"install": "true"}}]}), [])
        self.assertEqual(fleet.validate(fleet.empty_config()), [])

    def test_shape(self):
        self.assertIn("top level", "\n".join(fleet.validate([])))
        self.assertIn("\"services\" must be an array", self.errs("x"))
        self.assertIn("unknown top-level", self.errs([], extra=1))

    def test_problems(self):
        cases = [
            (svc(name="bad name"), "\"name\""),
            (svc(name="all"), "reserved"),
            (svc(label=""), "\"label\""),
            (svc(port=0), "port"),
            (svc(port="80"), "port"),
            (svc(port=True), "port"),
            (svc(probe="health"), "probe"),
            (svc(url_from_log="(", log="/x"), "not a valid regex"),
            (svc(url_from_log="x"), "needs \"log\""),
            (svc(prot=1), "unknown key(s): prot"),
            (svc(adopt={"install": "x"}), "exactly one"),
            ({"name": "a", "label": "local.a"}, "exactly one"),
            (svc(run={"argv": []}), "run.argv"),
            (svc(run={"argv": ["/bin/x"], "env": {"A": 1}}), "run.env"),
            (svc(run={"argv": ["/bin/x"], "keepAlive": "yes"}), "keepAlive"),
            (svc(run={"argv": ["/bin/x"], "cmd": "y"}), "run: unknown key"),
            ({"name": "a", "label": "local.a", "adopt": {"instal": "x"}}, "adopt: unknown key"),
            (svc(group="all"), "reserved"),
        ]
        for s, needle in cases:
            with self.subTest(s=s):
                self.assertIn(needle, self.errs([s]))

    def test_duplicates_and_group_collision(self):
        self.assertIn("duplicate name", self.errs([svc(), svc(label="local.other")]))
        self.assertIn("also used by", self.errs([svc(), svc(name="other")]))
        self.assertIn("same name as a service",
                      self.errs([svc(), svc(name="b", label="local.b", group="web")]))


class Groups(unittest.TestCase):
    CFG = {"services": [
        {"name": "a", "label": "l.a", "group": "g1", "adopt": {}},
        {"name": "b", "label": "l.b", "group": "g2", "adopt": {}},
        {"name": "c", "label": "l.c", "group": "g1", "adopt": {}},
        {"name": "d", "label": "l.d", "adopt": {}},
    ]}

    def names(self, *args):
        return [s["name"] for s in fleet.resolve(self.CFG, list(args))]

    def test_resolution(self):
        self.assertEqual(self.names(), ["a", "b", "c", "d"])
        self.assertEqual(self.names("all"), ["a", "b", "c", "d"])
        self.assertEqual(self.names("g1"), ["a", "c"])
        self.assertEqual(self.names("d", "g1"), ["a", "c", "d"])  # config order
        self.assertEqual(self.names("a", "g1", "a"), ["a", "c"])  # deduped

    def test_unknown(self):
        with self.assertRaises(fleet.FleetError) as cm:
            fleet.resolve(self.CFG, ["nope"])
        self.assertIn("groups: g1, g2", str(cm.exception))


class UrlFromLog(unittest.TestCase):
    def setUp(self):
        fd, self.log = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write("boot\ndemo web: http://127.0.0.1:5000/?token=OLD-1\nnoise\n"
                    "demo web: http://127.0.0.1:5000/?token=NEW_2\nmore\n")

    def tearDown(self):
        os.remove(self.log)

    def test_last_match_wins(self):
        rx = r"http://127\.0\.0\.1:5000/\?token=[A-Za-z0-9_-]+"
        self.assertEqual(fleet.url_from_log(rx, self.log), "http://127.0.0.1:5000/?token=NEW_2")

    def test_group_one(self):
        self.assertEqual(fleet.url_from_log(r"demo web: (\S+)", self.log), "http://127.0.0.1:5000/?token=NEW_2")

    def test_fallbacks(self):
        s = {"name": "x", "label": "l.x", "port": 5000, "log": self.log, "url_from_log": "nomatch",
             "run": {"argv": ["/bin/x"]}}
        self.assertEqual(fleet.service_url(s), "http://127.0.0.1:5000/")
        s["url"] = "http://h:1/?q=1"
        self.assertEqual(fleet.service_url(s), "http://h:1/?q=1")
        s["log"] = "/nonexistent/fleet.log"
        s["url_from_log"] = "x"
        self.assertEqual(fleet.service_url(s), "http://h:1/?q=1")
        del s["port"], s["url"]
        self.assertEqual(fleet.service_url(s), "")


class Cli(Sandbox):
    def test_empty_first_run(self):
        p = self.run_fleet("ls")
        self.assertIn("wrote", p.stderr)
        cfg = self.read_cfg()
        self.assertEqual(cfg, {"version": 1, "services": []})
        self.assertEqual(p.stdout, "")
        self.assertEqual(os.listdir(self.agents), [])  # first run writes no plists
        self.assertFalse(os.path.exists(self.state))  # ... and never calls launchctl
        self.assertNotIn("wrote", self.run_fleet("ls").stderr)

    def test_default_home_config_starts_empty(self):
        self.env.pop("FLEET_CONFIG")
        self.run_fleet("ls")
        path = os.path.join(self.tmp, ".config", "fleet", "services.json")
        with open(path) as f:
            self.assertEqual(json.load(f), {"version": 1, "services": []})
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(os.listdir(self.agents), [])

    def test_existing_generic_config_is_not_replaced(self):
        services = [svc(group="demo"), {"name": "adopted", "label": "local.adopted",
                                       "adopt": {"install": "true"}}]
        self.write_cfg(services)
        with open(self.cfg, "rb") as f:
            before = f.read()
        p = self.run_fleet("ls")
        self.assertIn("adopted", p.stdout)
        self.assertNotIn("wrote", p.stderr)
        self.assertEqual(self.read_cfg()["services"], services)
        with open(self.cfg, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertFalse(os.path.exists(self.state))

    def test_missing_config_no_create_mode(self):
        with self.assertRaises(fleet.FleetError):
            fleet.load_config(self.cfg, migrate=False)
        self.assertFalse(os.path.exists(self.cfg))

    def test_invalid_config_is_reported(self):
        os.makedirs(os.path.dirname(self.cfg))
        with open(self.cfg, "w") as f:
            f.write("{nope")
        p = self.run_fleet("status", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("not valid JSON", p.stderr)
        self.write_cfg([svc(port=99999)])
        p = self.run_fleet("status", check=False)
        self.assertIn("service 'web': \"port\" must be an integer", p.stderr)

    def test_add_up_down_remove_cycle(self):
        self.write_cfg([])
        self.run_fleet("add", "demo", "--group", "toys", "--env", "A=b&c", "--", "sleep", "60")
        entry = self.read_cfg()["services"][0]
        self.assertEqual(entry["label"], "local.demo")
        self.assertEqual(entry["run"]["argv"], [shutil.which("sleep"), "60"])
        plist = os.path.join(self.agents, "local.demo.plist")
        self.assertFalse(os.path.exists(plist))

        out = self.run_fleet("up", "toys").stdout
        self.assertRegex(out, r"demo\s+running\s+4242")
        with open(plist, "rb") as f:
            self.assertEqual(plistlib.load(f)["EnvironmentVariables"]["A"], "b&c")

        out = self.run_fleet("down", "demo").stdout
        self.assertRegex(out, r"demo\s+off")
        st = self.lstate()
        self.assertNotIn("local.demo", st["loaded"])
        self.assertIn("local.demo", st["disabled"])

        self.run_fleet("up", "demo")  # re-enables a disabled service
        self.assertNotIn("local.demo", self.lstate()["disabled"])
        self.run_fleet("remove", "demo")
        self.assertFalse(os.path.exists(plist))
        self.assertEqual(self.read_cfg()["services"], [])
        self.assertNotIn("local.demo", self.lstate()["loaded"])

    def test_up_does_not_regenerate_existing_plist_but_reinstall_does(self):
        self.write_cfg([svc()])
        plist = os.path.join(self.agents, "local.web.plist")
        with open(plist, "wb") as f:
            plistlib.dump({"Label": "local.web", "ProgramArguments": ["/old"]}, f)
        self.run_fleet("up", "web")
        with open(plist, "rb") as f:
            self.assertEqual(plistlib.load(f)["ProgramArguments"], ["/old"])
        self.run_fleet("reinstall", "web")
        with open(plist, "rb") as f:
            self.assertEqual(plistlib.load(f)["ProgramArguments"], ["/bin/echo", "hi"])

    def test_adopt_with_installer_that_loads_itself(self):
        plist = os.path.join(self.agents, "local.inst.plist")
        script = os.path.join(self.tmp, "install.py")
        with open(script, "w") as f:
            f.write("import plistlib, subprocess, sys\n"
                    "plistlib.dump({'Label': 'local.inst', 'ProgramArguments': ['/bin/true']}, open(%r, 'wb'))\n"
                    "subprocess.check_call([%r, 'bootstrap', 'gui/1', %r])\n" % (plist, FAKE, plist))
        self.write_cfg([])
        self.run_fleet("add", "inst", "--label", "local.inst", "--port", "1",
                       "--install", "%s %s" % (sys.executable, script))
        out = self.run_fleet("up", "inst").stdout
        self.assertRegex(out, r"inst\s+running")
        self.run_fleet("remove", "inst")
        self.assertTrue(os.path.exists(plist))  # adopted plists are never deleted

    def test_adopt_existing_label(self):
        plist = os.path.join(self.agents, "local.other.thing.plist")
        with open(plist, "wb") as f:
            plistlib.dump({"Label": "local.other.thing", "ProgramArguments": ["/bin/true"],
                           "StandardErrorPath": self.tmp + "/x/err.log"}, f)
        self.write_cfg([])
        self.run_fleet("adopt", "local.other.thing", "--port", "9", "--group", "misc")
        e = self.read_cfg()["services"][0]
        self.assertEqual(e, {"name": "thing", "label": "local.other.thing", "log": "~/x/err.log",
                             "port": 9, "group": "misc", "adopt": {}})
        self.assertFalse(os.path.exists(self.state) and self.lstate()["calls"][1:])  # read-only
        p = self.run_fleet("adopt", "local.nope", check=False)
        self.assertIn("no LaunchAgent", p.stderr)
        p = self.run_fleet("reinstall", "thing", check=False)
        self.assertIn("will not delete", p.stderr)
        self.assertTrue(os.path.exists(plist))

    def test_add_refuses_to_clobber_unmanaged_plist(self):
        with open(os.path.join(self.agents, "local.mine.plist"), "wb") as f:
            plistlib.dump({"Label": "local.mine"}, f)
        self.write_cfg([])
        p = self.run_fleet("add", "mine", "--", "/bin/true", check=False)
        self.assertIn("fleet adopt local.mine", p.stderr)
        p = self.run_fleet("add", "x", check=False)
        self.assertIn("exactly one", p.stderr)

    def test_unmanaged_agents_untouched(self):
        self.write_cfg([svc()])
        self.run_fleet("up")
        self.run_fleet("down", "all")
        labels = {c[-1].split("/")[-1] for c in self.lstate()["calls"] if c[0] != "bootstrap"}
        self.assertEqual(labels, {"local.web"})

    def test_url_and_logs(self):
        log = os.path.join(self.tmp, "w.log")
        with open(log, "w") as f:
            f.write("listening on http://127.0.0.1:5000/?k=abc\nline2\n")
        self.write_cfg([svc(port=5000, log=log, url_from_log=r"http://\S+"),
                        svc(name="quiet", label="local.quiet")])
        self.assertEqual(self.run_fleet("url", "web").stdout, "http://127.0.0.1:5000/?k=abc\n")
        p = self.run_fleet("url", "quiet", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("line2", self.run_fleet("logs", "web").stdout)

    def test_status_columns(self):
        self.write_cfg([svc(port=1)])
        out = self.run_fleet().stdout.splitlines()
        self.assertEqual(out[0].split(), ["SERVICE", "STATE", "PID", "HTTP", "URL"])
        self.assertEqual(out[1].split(), ["web", "absent", "-", "-", "http://127.0.0.1:1/"])


if __name__ == "__main__":
    unittest.main()
