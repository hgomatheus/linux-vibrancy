# Maintainer: hgomatheus
#
# Builds the checkout this file lives in: run `makepkg -si` from the repository root.
# The version is fixed here (no pkgver()) so makepkg never edits this tracked file
# and `git pull` keeps working.
pkgname=linux-vibrancy
pkgver=0.1.0
pkgrel=1
pkgdesc="NVIDIA-style digital vibrance (colour saturation) slider for KDE Plasma, Hyprland and X11"
arch=('any')
url="https://github.com/hgomatheus/linux-vibrancy"
license=('unknown')
depends=('python' 'python-pyqt6')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
checkdepends=('python-pytest')
optdepends=('nvidia-settings: X11 backend for NVIDIA cards'
            'xorg-xrandr: X11 backend for AMD and Intel cards')

build() {
  cd "$startdir"
  python -m build --wheel --no-isolation --outdir "$srcdir/dist"
}

check() {
  cd "$startdir"
  PYTHONPATH="$startdir" python -m pytest -q -p no:cacheprovider tests
}

package() {
  cd "$startdir"
  python -m installer --destdir="$pkgdir" "$srcdir"/dist/*.whl
  install -Dm644 data/linux-vibrancy.desktop "$pkgdir/usr/share/applications/linux-vibrancy.desktop"
}
