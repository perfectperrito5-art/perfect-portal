#!/usr/bin/env bash
# Unblock apt by removing the broken 7.0.0-28 kernel packages, and keep
# the working server kernel: 6.17.0-35-generic.
#
# Usage:
#   sudo ./gateway/fix-kernel-apt.sh

set -euo pipefail

KEEP_KERNEL="${KEEP_KERNEL:-6.17.0-35-generic}"
BAD_KERNEL="${BAD_KERNEL:-7.0.0-28-generic}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run with sudo:  sudo $0" >&2
  exit 1
fi

echo "Running kernel: $(uname -r)"
echo "Keeping:        $KEEP_KERNEL"
echo "Removing:       $BAD_KERNEL (+ HWE meta that pulls it)"
echo

if [[ "$(uname -r)" != "$KEEP_KERNEL" ]]; then
  echo "WARNING: you are not currently booted into $KEEP_KERNEL."
  echo "Boot that kernel from GRUB first if you can, then re-run this script."
  echo
fi

# Stop apt from getting stuck on VirtualBox DKMS for the bad kernel
if command -v dkms >/dev/null 2>&1; then
  dkms remove "virtualbox/7.0.16" -k "$BAD_KERNEL" --all 2>/dev/null || true
fi

# Remove unfinished / broken 7.x packages (force if half-configured)
for pkg in \
  "linux-headers-${BAD_KERNEL}" \
  "linux-image-${BAD_KERNEL}" \
  "linux-modules-${BAD_KERNEL}" \
  "linux-modules-extra-${BAD_KERNEL}" \
  "linux-headers-generic-hwe-24.04" \
  "linux-generic-hwe-24.04"
do
  if dpkg -l "$pkg" 2>/dev/null | grep -qE '^[a-z]'; then
    echo "Removing $pkg ..."
    dpkg --remove --force-remove-reinstreq "$pkg" 2>/dev/null \
      || dpkg --purge --force-all "$pkg" 2>/dev/null \
      || true
  fi
done

# HWE image meta currently points at 7.0 — remove so apt does not re-pull it
if dpkg -l linux-image-generic-hwe-24.04 2>/dev/null | grep -qE '^i'; then
  echo "Removing linux-image-generic-hwe-24.04 (pulls kernel 7.0) ..."
  apt-get remove -y linux-image-generic-hwe-24.04 2>/dev/null || \
    dpkg --remove --force-remove-reinstreq linux-image-generic-hwe-24.04 || true
fi

# Prefer / hold the working kernel
apt-mark hold \
  "linux-image-${KEEP_KERNEL}" \
  "linux-headers-${KEEP_KERNEL}" \
  2>/dev/null || true

# Block accidental reinstall of the bad kernel
apt-mark hold \
  "linux-image-${BAD_KERNEL}" \
  "linux-headers-${BAD_KERNEL}" \
  linux-generic-hwe-24.04 \
  linux-headers-generic-hwe-24.04 \
  linux-image-generic-hwe-24.04 \
  2>/dev/null || true

# Clear leftover configure errors
dpkg --configure -a || true
apt-get -f install -y || true

echo
echo "Held packages:"
apt-mark showhold || true
echo
echo "Installed kernels (images):"
dpkg -l 'linux-image-*-generic' 2>/dev/null | grep -E '^i' || true
echo
echo "Done. Apt should no longer touch $BAD_KERNEL."
echo "Next:  sudo ./gateway/start-ap.sh"