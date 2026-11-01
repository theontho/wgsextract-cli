ARG BUILDER_IMAGE=wgsextract/deepvariant-builder:1.10.0-arm64
FROM ${BUILDER_IMAGE}
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
