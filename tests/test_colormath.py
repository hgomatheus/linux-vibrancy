import io
import struct

import pytest

from vibrance import colormath as cm


def test_level_scale_matches_nvidia_control_panel():
    assert cm.level_to_nvidia(50) == 0
    assert cm.level_to_nvidia(0) == -1024
    assert cm.level_to_nvidia(100) == 1023
    assert cm.level_to_nvidia(75) == 512
    for level in range(0, 101):
        assert cm.nvidia_to_level(cm.level_to_nvidia(level)) == level


def test_saturation_matrix_keeps_greys_and_luminance():
    for s in (0.0, 0.5, 1.0, 1.7, 2.0):
        m = cm.saturation_matrix(s)
        grey = cm.mat_vec(m, (0.4, 0.4, 0.4))
        assert grey == pytest.approx((0.4, 0.4, 0.4))
        colour = (0.8, 0.3, 0.1)
        luma = sum(w * c for w, c in zip(cm.LUMA, colour))
        out = cm.mat_vec(m, colour)
        assert sum(w * c for w, c in zip(cm.LUMA, out)) == pytest.approx(luma)


def test_saturation_matrices_compose():
    a, b = cm.saturation_matrix(1.6), cm.saturation_matrix(1 / 1.6)
    ident = cm.mat_mul(a, b)
    for i in range(3):
        for j in range(3):
            assert ident[i][j] == pytest.approx(1.0 if i == j else 0.0, abs=1e-12)


def test_luma_is_rec709():
    assert cm.LUMA == pytest.approx((0.2126, 0.7152, 0.0722), abs=1e-4)


def test_ctm_encoding():
    assert cm.ctm_property_values(cm.saturation_matrix(1.0)) == [0, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1]
    values = cm.ctm_property_values(((-0.25, 1.5, 0), (0, 0, 0), (0, 0, 0)))
    assert all(-(2**31) <= v < 2**31 for v in values)
    # -0.25: magnitude 0x40000000 in the low word, sign bit in the high word
    assert values[0:2] == [0x40000000, -(2**31)]
    # 1.5: low word 0x80000000 (as signed), high word 1
    assert values[2:4] == [-(2**31), 1]


def test_shader_is_identity_at_default():
    src = cm.hyprland_shader(50)
    assert src.startswith("#version 300 es")
    assert "SATURATION = 1.000000" in src
    assert "SATURATION = 2.000000" in cm.hyprland_shader(100)


def _icc_tags(profile: bytes) -> dict[bytes, bytes]:
    (count,) = struct.unpack(">I", profile[128:132])
    tags = {}
    for i in range(count):
        sig, offset, size = struct.unpack(">4sII", profile[132 + 12 * i: 144 + 12 * i])
        tags[sig] = profile[offset: offset + size]
    return tags


def _xyz(tag: bytes):
    return tuple(v / 65536 for v in struct.unpack(">3i", tag[8:20]))


def _sf32(tag: bytes):
    v = [x / 65536 for x in struct.unpack(">9i", tag[8:44])]
    return (tuple(v[0:3]), tuple(v[3:6]), tuple(v[6:9]))


@pytest.mark.parametrize("level", [cm.min_icc_level(), 40, 60, 75, 100])
def test_icc_profile_reproduces_saturation_the_way_kwin_reads_it(level):
    """Re-derive the transform the way KWin's IccProfile::load + ColorPipeline do."""
    profile = cm.build_icc_profile(level)
    assert struct.unpack(">I", profile[:4])[0] == len(profile)
    assert profile[12:20] == b"mntrRGB " and profile[20:24] == b"XYZ " and profile[36:40] == b"acsp"
    tags = _icc_tags(profile)
    assert {b"desc", b"cprt", b"wtpt", b"chad", b"rXYZ", b"gXYZ", b"bXYZ", b"rTRC", b"gTRC", b"bTRC"} <= set(tags)

    undo = cm.mat_inv(_sf32(tags[b"chad"]))
    white = cm.mat_vec(undo, cm.D50_PCS)
    assert cm.xyz_to_xy(white) == pytest.approx(cm.D65_XY, abs=2e-4)
    cols = [cm.mat_vec(undo, _xyz(tags[t])) for t in (b"rXYZ", b"gXYZ", b"bXYZ")]
    declared = tuple(tuple(cols[j][i] for j in range(3)) for i in range(3))

    effective = cm.mat_mul(cm.mat_inv(declared), cm.SRGB_TO_XYZ)
    expected = cm.saturation_matrix(cm.level_to_saturation(level))
    for i in range(3):
        for j in range(3):
            assert effective[i][j] == pytest.approx(expected[i][j], abs=2e-3)

    trc = tags[b"rTRC"]
    assert trc[:4] == b"para" and struct.unpack(">H", trc[8:10])[0] == 0
    assert struct.unpack(">i", trc[12:16])[0] / 65536 == pytest.approx(2.2, abs=1e-4)


def test_icc_profile_through_littlecms():
    """LittleCMS (what KWin uses) must turn the profile into exactly Sat(s)."""
    ImageCms = pytest.importorskip("PIL.ImageCms")
    from PIL import Image

    def load(level):
        return ImageCms.ImageCmsProfile(io.BytesIO(cm.build_icc_profile(level)))

    samples = [(128, 128, 128), (255, 255, 255), (12, 12, 12), (200, 120, 100), (60, 90, 200), (230, 200, 180)]
    for level in (cm.min_icc_level(), 70, 100):
        transform = ImageCms.buildTransform(load(50), load(level), "RGB", "RGB",
                                            renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
        image = Image.new("RGB", (len(samples), 1))
        image.putdata(samples)
        result = ImageCms.applyTransform(image, transform)
        got = [result.getpixel((x, 0)) for x in range(len(samples))]
        s = cm.level_to_saturation(level)
        for rgb, out in zip(samples, got):
            linear = cm.mat_vec(cm.saturation_matrix(s), tuple((v / 255) ** 2.2 for v in rgb))
            want = tuple(round(255 * min(1.0, max(0.0, v)) ** (1 / 2.2)) for v in linear)
            assert max(abs(a - b) for a, b in zip(out, want)) <= 1, (level, rgb, out, want)


def test_kwin_validity_floor():
    floor = cm.min_icc_level()
    assert 0 < floor < 50
    assert not cm.kwin_accepts(0)
    assert all(cm.kwin_accepts(level) for level in range(floor, 101))
    with pytest.raises(ValueError):
        cm.build_icc_profile(floor - 1)


def test_profiles_are_reproducible():
    assert cm.build_icc_profile(80) == cm.build_icc_profile(80)
    assert cm.build_icc_profile(80) != cm.build_icc_profile(81)
