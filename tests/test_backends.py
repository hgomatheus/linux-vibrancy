from pathlib import Path

import pytest

from vibrance import backends, cli, colormath
from vibrance.backends import (
    BackendError,
    HyprlandBackend,
    KWinBackend,
    NvidiaX11Backend,
    Result,
    XrandrCtmBackend,
    detect_backend,
    parse_kscreen_outputs,
)
from vibrance.config import Config

Y, G, B, CR = "\x1b[01;33m", "\x1b[01;32m", "\x1b[01;34m", "\x1b[0;0m"


@pytest.fixture(autouse=True)
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    return tmp_path


def kscreen_block(name, connector="DisplayPort", icc="", source="sRGB", rgb="Automatic", hdr="incapable", wcg="incapable"):
    """Same layout and colour codes as kscreen-doctor's showOutputs()."""
    return (
        f"{G}Output: {CR}1 {name} 0b2c5f0e-1111-2222-3333-444455556666\n"
        f"\t{G}enabled{CR}\n"
        f"\t{G}connected{CR}\n"
        f"\t{G}priority {CR}1\n"
        f"\t{Y}{connector}{CR}\n"
        f"\t{Y}replication source:{CR}0\n"
        f"\t{B}Modes: {CR} 1:{G}2560x1440@165.00*{CR}! 2:1920x1080@60.00 \n"
        f"{Y}\tCustom modes:{CR} None\n"
        f"\t{Y}Geometry: {CR}0,0 2560x1440\n"
        f"\t{Y}Scale: {CR}1\n"
        f"{Y}\tRgbRange: {CR}{rgb}\n"
        f"{Y}\tHDR: {CR}{hdr}\n"
        f"{Y}\tWide Color Gamut: {CR}{wcg}\n"
        f"{Y}\tICC profile: {CR}{icc or 'none'}\n"
        f"{Y}\tColor profile source: {CR}{source}\n"
        f"{Y}\tColor power preference: {CR}prefer efficiency and performance\n"
    )


class FakeKScreen:
    """Behaves like kscreen-doctor talking to KWin, including its '.'-splitting quirk."""

    def __init__(self, **outputs):
        self.state = outputs
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        assert cmd[0] == "kscreen-doctor"
        if cmd[1:] == ["-o"]:
            return Result(0, "".join(kscreen_block(name, **o) for name, o in self.state.items()))
        for arg in cmd[1:]:
            ops = arg.split(".")
            out = self.state.get(ops[1])
            if out is None:
                return Result(3, "")
            if ops[2] == "iccprofile":
                out["icc"] = ".".join(ops[3:])
            elif ops[2] == "colorProfileSource":
                assert ops[3] in ("sRGB", "ICC", "EDID")
                out["source"] = ops[3]
        return Result(0, "")


def test_parse_kscreen_outputs():
    text = kscreen_block("DP-1") + kscreen_block("HDMI-A-1", connector="HDMI", icc="/x/y.icc", source="ICC", rgb="Limited", hdr="enabled")
    outs = parse_kscreen_outputs(text)
    assert list(outs) == ["DP-1", "HDMI-A-1"]
    dp, hdmi = outs["DP-1"], outs["HDMI-A-1"]
    assert dp.connected and dp.enabled and dp.connector == "DisplayPort"
    assert dp.icc_profile == "" and dp.profile_source == "sRGB" and not dp.hdr
    assert hdmi.icc_profile == "/x/y.icc" and hdmi.profile_source == "ICC"
    assert hdmi.rgb_range == "Limited" and hdmi.hdr and hdmi.connector == "HDMI"


def test_kwin_apply_and_restore_default():
    fake = FakeKScreen(**{"DP-1": {}})
    config = Config()
    kwin = KWinBackend(config, fake)

    assert kwin.apply("DP-1", 75) == []
    icc = Path(fake.state["DP-1"]["icc"])
    assert icc.parent == KWinBackend.icc_dir() and icc.name == "vibrance-75.icc"
    assert icc.read_bytes() == colormath.build_icc_profile(75)
    assert fake.state["DP-1"]["source"] == "ICC"
    assert kwin.current_level("DP-1") == 75

    kwin.apply("DP-1", 60)
    assert fake.state["DP-1"]["icc"].endswith("vibrance-60.icc")
    assert config.section("kwin_original")["DP-1"] == {"source": "sRGB", "icc": ""}

    kwin.apply("DP-1", 50)
    assert fake.state["DP-1"] == {"icc": "", "source": "sRGB"}
    assert kwin.current_level("DP-1") == 50
    assert "DP-1" not in config.section("kwin_original")


def test_kwin_puts_back_users_own_profile():
    fake = FakeKScreen(**{"DP-1": {"icc": "/home/me/calibrated.v2.icc", "source": "ICC"}})
    kwin = KWinBackend(Config(), fake)
    notes = kwin.apply("DP-1", 70)
    assert any("calibrated.v2.icc" in n for n in notes)
    assert fake.state["DP-1"]["icc"].endswith("vibrance-70.icc")
    kwin.apply("DP-1", 50)
    assert fake.state["DP-1"] == {"icc": "/home/me/calibrated.v2.icc", "source": "ICC"}


def test_kwin_remembers_original_across_runs():
    fake = FakeKScreen(**{"DP-1": {"source": "EDID"}})
    config = Config()
    KWinBackend(config, fake).apply("DP-1", 80)
    config.save()
    KWinBackend(Config(), fake).apply("DP-1", 50)
    assert fake.state["DP-1"]["source"] == "EDID"


def test_kwin_leaves_user_choice_alone_at_default():
    fake = FakeKScreen(**{"DP-1": {"source": "EDID"}})
    KWinBackend(Config(), fake).apply("DP-1", 50)
    assert [c for c in fake.calls if c[1:] != ["-o"]] == []


def test_kwin_clamps_and_warns():
    fake = FakeKScreen(**{"DP-1": {"hdr": "enabled"}})
    notes = KWinBackend(Config(), fake).apply("DP-1", 5)
    floor = colormath.min_icc_level()
    assert fake.state["DP-1"]["icc"].endswith(f"vibrance-{floor}.icc")
    assert any("can't desaturate" in n for n in notes)
    assert any("HDR" in n for n in notes)


def test_kwin_unknown_output():
    with pytest.raises(BackendError):
        KWinBackend(Config(), FakeKScreen(**{"DP-1": {}})).apply("DP-9", 70)


def test_kwin_diagnose():
    fake = FakeKScreen(**{
        "DP-1": {"rgb": "Limited"},
        "HDMI-A-1": {"connector": "HDMI", "source": "EDID"},
    })
    found = "\n".join(KWinBackend(Config(), fake).diagnose())
    assert "output.DP-1.rgbrange.full" in found
    assert "output.HDMI-A-1.rgbrange.full" in found
    assert "output.HDMI-A-1.colorProfileSource.sRGB" in found


class Scripted:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        return self.handler(cmd)


def test_hyprland_keyword():
    run = Scripted(lambda cmd: Result(0, "ok"))
    hypr = HyprlandBackend(Config(), run)
    hypr.apply("all", 80)
    path = str(HyprlandBackend.shader_path())
    assert run.calls == [
        ["hyprctl", "keyword", "decoration:screen_shader", "[[EMPTY]]"],
        ["hyprctl", "keyword", "decoration:screen_shader", path],
    ]
    assert "SATURATION = 1.600000" in Path(path).read_text()
    run.calls.clear()
    hypr.apply("all", 50)
    assert run.calls == [["hyprctl", "keyword", "decoration:screen_shader", "[[EMPTY]]"]]


def test_hyprland_lua_config():
    def handler(cmd):
        return Result(0, "ok" if cmd[1] == "eval" else "unknown request")

    run = Scripted(handler)
    hypr = HyprlandBackend(Config(), run)
    hypr.apply("all", 70)
    path = HyprlandBackend.shader_path()
    assert run.calls[-1] == ["hyprctl", "eval", f'hl.config({{ decoration = {{ screen_shader = "{path}" }} }})']
    run.calls.clear()
    hypr.apply("all", 60)
    assert [c[1] for c in run.calls] == ["eval", "eval"]


def test_hyprland_error():
    with pytest.raises(BackendError):
        HyprlandBackend(Config(), Scripted(lambda cmd: Result(1, "", "no socket"))).apply("all", 70)


NV_DPYS = """
2 Display Devices on archbox:0

    [0] archbox:0[dpy:1] (DP-2) (connected, enabled)

      Has the following names:
        DFP
        DFP-1
        DPY-1
        DP-2

    [1] archbox:0[dpy:4] (HDMI-0) (connected)

      Has the following names:
        DFP-4
        HDMI-0
"""


def test_nvidia_backend():
    values = {"DigitalVibrance": "512", "ColorRange": "1", "ColorSpace": "0"}

    def handler(cmd):
        if cmd[1:] == ["-q", "dpys"]:
            return Result(0, NV_DPYS)
        if cmd[1:3] == ["-t", "-q"]:
            return Result(0, values[cmd[3].split("/")[1]] + "\n")
        return Result(0, "  Attribute 'DigitalVibrance' (archbox:0[dpy:1]) assigned value 512.\n")

    run = Scripted(handler)
    nv = NvidiaX11Backend(Config(), run)
    assert [o.name for o in nv.outputs()] == ["DP-2"]
    nv.apply("DP-2", 100)
    assert run.calls[-1] == ["nvidia-settings", "-a", "[DPY:DP-2]/DigitalVibrance=1023"]
    assert nv.current_level("DP-2") == 75
    found = nv.diagnose()
    assert len(found) == 1 and "ColorRange=0" in found[0]


XRANDR_PROP = """Screen 0: minimum 320 x 200, current 2560 x 1440, maximum 16384 x 16384
DisplayPort-0 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 597mm x 336mm
\tEDID:
\t\t00ffffffffffff0010ac
\tCTM: 0 1 0 0 0 0 0 0 0 1 0 0 0 0 0 0
\t\t0 1
\tvrr_capable: 1
   2560x1440    164.96*+  59.95
HDMI-A-0 connected (normal left inverted right x axis y axis)
\tCTM: 0 1 0 0 0 0 0 0 0 1 0 0 0 0 0 0
DVI-D-0 disconnected (normal left inverted right x axis y axis)
"""


def test_xrandr_backend():
    run = Scripted(lambda cmd: Result(0, XRANDR_PROP if cmd[1] == "--prop" else ""))
    xr = XrandrCtmBackend(Config(), run)
    assert [o.name for o in xr.outputs()] == ["DisplayPort-0"]
    xr.apply("DisplayPort-0", 50)
    assert run.calls[-1] == ["xrandr", "--output", "DisplayPort-0", "--set", "CTM",
                             "0,1,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0,1"]
    xr.apply("DisplayPort-0", 80)
    values = [int(v) for v in run.calls[-1][-1].split(",")]
    assert len(values) == 18 and values != [0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1]


def test_detect_backend(monkeypatch):
    monkeypatch.setattr(backends.shutil, "which", lambda name: f"/usr/bin/{name}")
    run = Scripted(lambda cmd: Result(0, ""))
    kde = {"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE"}
    assert isinstance(detect_backend(Config(), run, env=kde), KWinBackend)
    hypr = {"XDG_SESSION_TYPE": "wayland", "HYPRLAND_INSTANCE_SIGNATURE": "abc"}
    assert isinstance(detect_backend(Config(), run, env=hypr), HyprlandBackend)
    with pytest.raises(BackendError, match="GNOME"):
        detect_backend(Config(), run, env={"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "GNOME"})

    nv = Scripted(lambda cmd: Result(0, NV_DPYS if cmd[0] == "nvidia-settings" else XRANDR_PROP))
    assert isinstance(detect_backend(Config(), nv, env={"DISPLAY": ":0"}), NvidiaX11Backend)
    amd = Scripted(lambda cmd: Result(1, "") if cmd[0] == "nvidia-settings" else Result(0, XRANDR_PROP))
    assert isinstance(detect_backend(Config(), amd, env={"XDG_SESSION_TYPE": "x11"}), XrandrCtmBackend)


def test_cli_set_status_reset(monkeypatch, capsys):
    fake = FakeKScreen(**{"DP-1": {}, "HDMI-A-1": {"connector": "HDMI"}})
    monkeypatch.setattr(cli, "detect_backend", lambda config, run, forced=None: KWinBackend(config, fake))

    assert cli.main(["set", "70", "-o", "HDMI-A-1"]) == 0
    assert fake.state["HDMI-A-1"]["icc"].endswith("vibrance-70.icc")
    assert fake.state["DP-1"].get("icc", "") == ""
    assert Config().level("kwin", "HDMI-A-1") == 70

    capsys.readouterr()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "HDMI-A-1" in out and "70%" in out and "50% (normal)" in out

    assert cli.main(["reset"]) == 0
    assert fake.state["HDMI-A-1"]["source"] == "sRGB"
    assert cli.main(["set", "60", "-o", "DP-7"]) == 1


def test_cli_rejects_bad_levels():
    with pytest.raises(SystemExit):
        cli.main(["set", "150"])
    with pytest.raises(SystemExit):
        cli.main(["set", "abc"])
    assert cli.level_arg("65%") == 65


def test_autostart_file(monkeypatch, capsys):
    monkeypatch.setattr(cli, "detect_backend", lambda *a, **k: HyprlandBackend(Config(), lambda cmd: Result(0, "ok")))
    assert cli.main(["autostart", "on"]) == 0
    out = capsys.readouterr().out
    assert "screen_shader" in out
    from vibrance.config import autostart_file
    text = autostart_file().read_text()
    assert "restore" in text and text.startswith("[Desktop Entry]")
    assert cli.main(["autostart", "off"]) == 0
    assert not autostart_file().exists()
