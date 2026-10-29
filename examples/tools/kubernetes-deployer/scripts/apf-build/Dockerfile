# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
# Dedicated APF compatibility images; build context is apf-openshell-0.0.116.
FROM rust@sha256:6258907abe69656e41cd992e0b705cdcfabcbbe3db374f92ed2d47121282d4a1 AS build
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler libprotobuf-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/usr/local/cargo/registry --mount=type=cache,target=/src/target cargo build --locked --release -p apf-policy-service -p apf-policy-gateway -p apf-compile && cp target/release/apf-policy-service target/release/apf-policy-gateway target/release/apf-compile /usr/local/bin/
RUN protoc --include_imports --include_source_info --proto_path=contracts/third_party/openshell/proto --descriptor_set_out=/official-openshell-0.0.116-descriptor.bin contracts/third_party/openshell/proto/*.proto

FROM debian@sha256:7ff6ba31527b9e336162fa061746c366c4b2032f4d2cff2078bea43f78efe3f8 AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/LICENSE /src/THIRD_PARTY_LICENSES.txt /usr/share/licenses/apf/
COPY --from=build /official-openshell-0.0.116-descriptor.bin /usr/share/apf/official-openshell-0.0.116-descriptor.bin
RUN mkdir -p /run/apf /run/apf-gateway /var/lib/apf /data/bundles /data/keys /etc/apf
LABEL org.opencontainers.image.source="https://github.com/NVIDIA/agent-policy-fabric" org.opencontainers.image.revision="fb961f7cc78e3d5149f0505ca624d064be1866f8" io.nvidia.apf.openshell-contract="d1155aa70042d3e2ee49dbfa15346b108b7c1d92" io.nvidia.apf.compatibility="official-0.0.116-dedicated"

FROM runtime AS policy-service
COPY --from=build /usr/local/bin/apf-policy-service /usr/local/bin/apf-compile /usr/local/bin/
ENTRYPOINT ["apf-policy-service", "serve"]

FROM runtime AS policy-gateway
COPY --from=build /usr/local/bin/apf-policy-gateway /usr/local/bin/
ENTRYPOINT ["apf-policy-gateway"]
