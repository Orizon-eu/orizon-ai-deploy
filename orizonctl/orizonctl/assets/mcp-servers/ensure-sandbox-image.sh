#!/bin/sh
# Ensures the sandbox runtime image is in the sandbox daemon's private store and
# holds what .env.mcp asks for, building it there when the tag is not pullable.
# Runs as the sandbox-image-loader entrypoint, with SANDBOX_IMAGE and
# SANDBOX_PACKAGES read from .env.mcp via env_file and DOCKER_HOST pointing at
# sandbox-dind.
#
# The image is rebuilt whenever either of the two things it is built FROM stops
# matching what the image itself records: SANDBOX_PACKAGES, and the Dockerfile.
# Both are labels on the image, so adding a library or editing the recipe is one
# edit plus `up -d`.
#
# The Dockerfile half is here because the packages label answered only half the
# question. Everything the recipe does that is not pip — the apt step, the env,
# the directory modes — was outside the comparison, so adding the document fonts
# to the apt line left the loader reporting "present and matches" against an
# image with no fonts in it. The prompt half of that change shipped, the image
# half did not, and every PDF then failed registering a font from a path the
# document prompt promised was there. A hash of the file the daemon is handed
# has no such blind spot: any edit to it is a different image.
#
# SANDBOX_IMAGE_REBUILD=true rebuilds regardless, with --no-cache --pull. That
# is the case no label can see: the recipe is unchanged and what it installs has
# moved underneath it, because apt or the base image published something new.
set -e

if [ -z "$SANDBOX_IMAGE" ] || [ -z "$SANDBOX_PACKAGES" ]; then
  echo "SANDBOX_IMAGE and SANDBOX_PACKAGES must both be set in .env.mcp." >&2
  exit 1
fi

RECIPE=/build/Dockerfile

if [ ! -f "$RECIPE" ]; then
  echo "$RECIPE is not mounted; the loader has nothing to build from." >&2
  exit 1
fi

# Compared as sets: ordering and comma spacing in .env.mcp are cosmetic and must
# not trigger a ~1GB rebuild.
normalize() {
  echo "$1" | tr ', ' '\n\n' | sed '/^$/d' | sort | tr '\n' ' '
}

label_of() {
  docker image inspect -f "{{index .Config.Labels \"$2\"}}" "$1" 2>/dev/null || true
}

case "${SANDBOX_IMAGE_REBUILD:-}" in
  true|TRUE|True|yes|YES|1) forced=yes ;;
  *) forced=no ;;
esac

want_packages=$(normalize "$SANDBOX_PACKAGES")
# The WHOLE build context, not just the Dockerfile.
#
# It used to be the Dockerfile alone, which was right while the Dockerfile was
# the only thing in the context. It no longer is: the helper package the
# generated scripts import and the script that writes down what the libraries
# export are both copied in, so both decide what the image contains, and a
# change to either with no Dockerfile edit would answer "matches" forever. That
# is the exact failure the recipe label was added for, one level along — the apt
# step for the fonts was outside the packages label, and an image built before
# it reported a match while every PDF failed registering a font.
#
# Sorted before hashing so `find` order cannot make two identical contexts hash
# differently and rebuild a gigabyte for nothing. Short, because it is read by a
# person deciding whether two images are the same one, not by anything needing
# collision resistance against an adversary.
want_recipe=$(
  find /build -type f 2>/dev/null \
    | LC_ALL=C sort \
    | xargs sha256sum \
    | sha256sum \
    | cut -c1-16
)

built_packages=$(label_of "$SANDBOX_IMAGE" orizon.sandbox.packages)
built_recipe=$(label_of "$SANDBOX_IMAGE" orizon.sandbox.recipe)

packages_match=no
[ -n "$built_packages" ] && [ "$(normalize "$built_packages")" = "$want_packages" ] && packages_match=yes
recipe_match=no
[ "$built_recipe" = "$want_recipe" ] && recipe_match=yes

if [ "$forced" = yes ]; then
  echo "SANDBOX_IMAGE_REBUILD is set; rebuilding $SANDBOX_IMAGE from scratch."
elif [ "$packages_match" = yes ] && [ "$recipe_match" = yes ]; then
  echo "$SANDBOX_IMAGE is present and matches SANDBOX_PACKAGES and the Dockerfile."
  exit 0
elif docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1; then
  echo "$SANDBOX_IMAGE was built from something else; rebuilding."
  if [ "$packages_match" = no ]; then
    echo "  packages, image:    ${built_packages:-<unlabelled>}"
    echo "  packages, .env.mcp: $SANDBOX_PACKAGES"
  fi
  if [ "$recipe_match" = no ]; then
    echo "  Dockerfile, image:  ${built_recipe:-<unlabelled>}"
    echo "  Dockerfile, on disk: $want_recipe"
  fi
elif docker pull "$SANDBOX_IMAGE" 2>/dev/null; then
  # A registry tag is whatever it was published as. Rebuilding it locally from
  # this Dockerfile would give the tag a second meaning, so this warns and stops.
  pulled_packages=$(label_of "$SANDBOX_IMAGE" orizon.sandbox.packages)
  pulled_recipe=$(label_of "$SANDBOX_IMAGE" orizon.sandbox.recipe)
  if [ -n "$pulled_packages" ] && [ "$(normalize "$pulled_packages")" != "$want_packages" ]; then
    echo "WARNING: pulled $SANDBOX_IMAGE holds $pulled_packages, not $SANDBOX_PACKAGES." >&2
    echo "run_python will be offered packages the image lacks. Bump SANDBOX_IMAGE" >&2
    echo "to a tag built from the current list." >&2
  fi
  if [ -n "$pulled_recipe" ] && [ "$pulled_recipe" != "$want_recipe" ]; then
    echo "WARNING: pulled $SANDBOX_IMAGE was built from Dockerfile $pulled_recipe," >&2
    echo "not the $want_recipe on disk. Anything the recipe installs outside pip —" >&2
    echo "the fonts above all — may not be in the image the prompts describe." >&2
    echo "Publish and bump SANDBOX_IMAGE, or set SANDBOX_IMAGE_REBUILD=true to" >&2
    echo "build this Dockerfile over the tag locally." >&2
  fi
  echo "Pulled $SANDBOX_IMAGE."
  exit 0
else
  echo "$SANDBOX_IMAGE is not in the sandbox daemon and is not pullable; building."
fi

# Built by the sandbox daemon rather than the host's: its store is the one that
# matters here, and handing this container the host socket would put a
# root-equivalent socket on the sandbox network. pip needs egress, which
# orizon-sandbox-network has and executed code does not.
#
# A forced build takes the layer cache away as well, since the whole point of
# asking for one is that the inputs look identical and the output is not.
cache=""
[ "$forced" = yes ] && cache="--no-cache --pull"

echo "Building $SANDBOX_IMAGE from: $SANDBOX_PACKAGES"
echo "Dockerfile $want_recipe"
# shellcheck disable=SC2086
docker build \
  $cache \
  --build-arg SANDBOX_PACKAGES="$SANDBOX_PACKAGES" \
  --build-arg SANDBOX_RECIPE="$want_recipe" \
  -t "$SANDBOX_IMAGE" \
  -f "$RECIPE" /build
echo "Built $SANDBOX_IMAGE."
