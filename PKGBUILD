# Maintainer: hgomatheus
pkgname=linux-vibrancy-git
_pkgname=linux-vibrancy
pkgver=0.1.0
pkgrel=1
pkgdesc="NVIDIA-style digital vibrance (colour saturation) slider for KDE Plasma, Hyprland and X11"
arch=('any')
url="https://github.com/hgomatheus/linux-vibrancy"
license=('unknown')
depends=('python' 'python-pyqt6')
makedepends=('git' 'python-build' 'python-installer' 'python-setuptools' 'python-wheel')
checkdepends=('python-pytest')
optdepends=('nvidia-settings: X11 backend for NVIDIA cards'
            'xorg-xrandr: X11 backend for AMD and Intel cards')
provides=("$_pkgname")
conflicts=("$_pkgname")
source=("git+$url.git")
sha256sums=('SKIP')

pkgver() {
  cd "$_pkgname"
  printf "0.1.0.r%s.g%s" "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)"
}

build() {
  cd "$_pkgname"
  python -m build --wheel --no-isolation
}

check() {
  cd "$_pkgname"
  PYTHONPATH="$PWD" python -m pytest -q
}

package() {
  cd "$_pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 data/linux-vibrancy.desktop "$pkgdir/usr/share/applications/linux-vibrancy.desktop"
}
