# The Plow cloud image: Watson, built for Plow to run as a cloud agent.
#
# Tag AND digest, the way the reference variant pins it. The `base-<sha>` tag
# names one commit of the base's source repo (plow-pbc/plow-hermes-agent) and
# is the provenance a human reads; the digest is what actually makes the pin
# hold. A tag is a registry-side pointer, and "we never move it" is a promise,
# not a mechanism -- every tenant VM inherits this filesystem while holding that
# owner's Plow credential, so a recreated tag would run substituted root code
# against live credentials. Resolved from the registry, not copied:
#   docker-content-digest: sha256:0c3892e9…90ff0 for base-67021a70…
FROM public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-67021a7029e33e80bcb27899be6515a5a0e9b37b@sha256:0c3892e93c1a001c61fb7106396e0a4b7e0219008184fd90719caa84a3390ff0

# Identity: only what is specific to this agent. plow-init writes the home's
# SOUL.md on every boot as the base persona followed by this file; nothing is
# COPYed to /var/lib/hermes/SOUL.md, which is overwritten at boot.
COPY image/persona.md /opt/hermes/plow-seed/persona.md
# The mode in its own step: `COPY --chmod=` is BuildKit-only, and a stock Docker
# still selects the legacy builder, where it fails the build outright.
RUN chmod 0644 /opt/hermes/plow-seed/persona.md

# Both copies, as the base image does. /var/lib/hermes/skills is what a home
# that starts empty is seeded from and what the gateway reads; /opt/hermes/skills
# is outside every home, so a bind-mounted home still receives it and an image
# update still reaches a skill the agent has not customised.
COPY --chown=10000:10000 image/skills/ /var/lib/hermes/skills/
COPY --chown=10000:10000 image/skills/ /opt/hermes/skills/

# `gh` is how watson.github reads the API and how `repair` opens its draft pull
# request. Pinned by version AND checksum, for the same reason the base pins the
# index client that way: this binary runs inside an agent holding a live
# credential, and a sha in a URL is only as good as the host serving it.
# linux/amd64 because that is what `plow-agents image build` produces.
ARG GH_VERSION=2.101.0
ARG GH_SHA256=9bca2d1c16825f109907a23307628a2f0698fbf99662b73a5cf0b020293072b8
RUN set -eu; \
    tarball="gh_${GH_VERSION}_linux_amd64.tar.gz"; \
    curl -fsS --max-time 120 -L -o "/tmp/$tarball" \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${tarball}"; \
    echo "${GH_SHA256}  /tmp/$tarball" | sha256sum -c -; \
    tar -xzf "/tmp/$tarball" -C /tmp; \
    install -m 0755 "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh; \
    rm -rf "/tmp/$tarball" "/tmp/gh_${GH_VERSION}_linux_amd64"; \
    gh --version

# Watson itself, into the runtime's own venv -- the one the cycle service calls
# by absolute path. `--no-deps` the way the base installs its one extra package:
# this project declares no runtime dependencies, and resolving any would let the
# install move a version the pinned base chose. The import check is the build's,
# so a boot that could not start Watson fails here instead.
#
# `--no-deps` does not disable build isolation, so the backend `pyproject.toml`
# asks for would otherwise resolve unpinned and run as root beside everything
# else here that is pinned by version and digest. The build constraint pins it.
# `--no-build-isolation` is not the alternative: the base's venv has no
# setuptools, so it fails outright. The constraint carries the wheel's sha256
# for the same reason `gh` above does: a version alone names a release, not the
# artifact that gets executed.
COPY pyproject.toml /opt/watson/pyproject.toml
COPY watson/ /opt/watson/watson/
RUN set -eu; \
    printf '%s\n' 'setuptools==80.9.0 --hash=sha256:062d34222ad13e0cc312a4c02d73f059e86a4acbfbdea8f8f76b28c99f306922' >/tmp/build-constraints.txt; \
    uv pip install --python /opt/hermes/.venv/bin/python --no-deps --build-constraints /tmp/build-constraints.txt /opt/watson; \
    /opt/hermes/.venv/bin/watson --help >/dev/null

# The boot layer. COPY merges into the base's tree, so its own `user` bundle
# entries survive alongside this one:
#
#   watson-cycle  longrun, one `watson cycle` every ten minutes, depends on plow-init
#
# No agent-index service here: the base already ships that reporter and the
# client it runs. A variant supplies AGENT_ID and nothing else.
COPY image/s6-overlay/ /etc/s6-overlay/
RUN chmod 0755 /etc/s6-overlay/s6-rc.d/watson-cycle/run

COPY LICENSE /usr/share/doc/watson/
