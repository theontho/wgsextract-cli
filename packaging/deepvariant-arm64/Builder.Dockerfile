FROM arm64v8/ubuntu:24.04
ARG DV_GPU_BUILD=0
ARG PYTHON_VERSION=3.10
ENV DV_GPU_BUILD=${DV_GPU_BUILD}
ENV PYTHON_VERSION=${PYTHON_VERSION}
ENV DV_BIN_PATH=/opt/deepvariant/bin
ENV TMPDIR=/opt/deepvariant/build-tmp
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv
RUN mkdir -p /opt/deepvariant/build-tmp && \
    uv venv /opt/venv --python 3.10 && \
    ln -sf /opt/venv/bin/python3 /usr/bin/python3 && \
    ln -sf /opt/venv/bin/python3 /usr/bin/python && \
    ln -sf /opt/venv/bin/python3 /usr/bin/python3.10
ENV VIRTUAL_ENV=/opt/venv
ENV PATH=/opt/venv/bin:/root/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
COPY . /opt/deepvariant
WORKDIR /opt/deepvariant
RUN chmod +x scripts/build/*.sh && \
    bash scripts/build/build-prereq-arm64.sh && \
    uv pip install --reinstall-package cryptography --reinstall-package cffi cryptography cffi "httplib2<0.22" && \
    uv pip install --no-deps jax==0.4.35 jaxlib==0.4.35 keras==3.12.1 \
        tf-keras==2.16.0 ml-dtypes==0.5.4 numpy==1.24.3 && \
    bash scripts/build/build_release_binaries_arm64.sh
