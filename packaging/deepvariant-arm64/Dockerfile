FROM arm64v8/ubuntu:24.04 AS builder
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

FROM builder AS runtime
LABEL org.opencontainers.image.source="https://github.com/theontho/wgsextract-cli" \
      org.opencontainers.image.version="1.10.0-arm64" \
      org.opencontainers.image.description="Native CPU build of Google's unchanged DeepVariant 1.10.0 scientific source"
RUN set -e; apt-get update -qq && apt-get install -y -qq parallel samtools bcftools file time libjemalloc2 && \
    python_root="$(dirname "$(dirname "$(readlink -f /opt/venv/bin/python3)")")" && \
    cp -a "$python_root" /opt/cpython && \
    for name in python python3 python3.10; do \
        ln -sf /opt/cpython/bin/python3.10 "/opt/venv/bin/${name}"; \
    done && \
    sed -i 's|^home = .*|home = /opt/cpython/bin|' /opt/venv/pyvenv.cfg && \
    mkdir -p /opt/deepvariant/bin && \
    for tool in make_examples call_variants postprocess_variants vcf_stats_report; do \
        cp "/opt/deepvariant/bazel-bin/deepvariant/${tool}.zip" "/opt/deepvariant/bin/${tool}.zip"; \
        unzip -p "/opt/deepvariant/bin/${tool}.zip" __main__.py | \
            sed "s|PYTHON_BINARY = '/usr/bin/python3'|PYTHON_BINARY = '/opt/venv/bin/python3'|" \
            > /opt/deepvariant/build-tmp/__main__.py; \
        grep -q "PYTHON_BINARY = '/opt/venv/bin/python3'" /opt/deepvariant/build-tmp/__main__.py; \
        (cd /opt/deepvariant/build-tmp && zip -q "/opt/deepvariant/bin/${tool}.zip" __main__.py); \
        printf '#!/bin/bash\n/opt/venv/bin/python3 /opt/deepvariant/bin/%s.zip "$@"\n' "$tool" \
            > "/opt/deepvariant/bin/${tool}"; \
        chmod +x "/opt/deepvariant/bin/${tool}"; \
    done && \
    cp /opt/deepvariant/scripts/run_deepvariant.py /opt/deepvariant/bin/run_deepvariant.py && \
    printf '#!/bin/bash\n/opt/venv/bin/python3 -u /opt/deepvariant/bin/run_deepvariant.py "$@"\n' \
        > /opt/deepvariant/bin/run_deepvariant && \
    chmod +x /opt/deepvariant/bin/run_deepvariant
RUN set -e; \
    base=https://storage.googleapis.com/deepvariant/models/DeepVariant/1.10.0; \
    for pair in wgs:wgs wes:wes pacbio:pacbio hybrid_pacbio_illumina:hybrid ont_r104:ont masseq:masseq rnaseq:rnaseq; do \
        model="${pair%%:*}"; slug="${pair##*:}"; \
        mkdir -p "/opt/models/${model}/variables"; \
        for file in fingerprint.pb saved_model.pb model.example_info.json variables/variables.data-00000-of-00001 variables/variables.index; do \
            curl --retry 3 -fsSL "${base}/savedmodels/deepvariant.${slug}.savedmodel/${file}" \
                -o "/opt/models/${model}/${file}"; \
        done; \
    done; \
    for pair in wgs:wgs pacbio:pacbio ont_r104:ont; do \
        model="${pair%%:*}"; slug="${pair##*:}"; \
        mkdir -p "/opt/smallmodels/${slug}"; \
        curl --retry 3 -fsSL "${base}/smallmodels/deepvariant.${slug}.smallmodel/model.keras" \
            -o "/opt/smallmodels/${slug}/model.keras"; \
    done; \
    find /opt/models /opt/smallmodels -type f -exec sha256sum {} + > /opt/deepvariant/model-sha256.txt
COPY model-sha256.txt /opt/deepvariant/expected-model-sha256.txt
RUN sha256sum -c /opt/deepvariant/expected-model-sha256.txt
ENV PYTHONPATH=/opt/venv/lib/python3.10/site-packages
ENV TF_ENABLE_ONEDNN_OPTS=1
ENV TF_USE_LEGACY_KERAS=1
ENV PATH=/opt/deepvariant/bin:/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENTRYPOINT ["/opt/deepvariant/bin/run_deepvariant"]
