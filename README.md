# Bullet Helm charts

Helm charts for running Bullet infrastructure components yourself.

| Chart | Description | Documentation |
| ----- | ----------- | ------------- |
| `hyperlane-bullet-validator` | Hyperlane checkpoint validator for the Bullet rollup: signs checkpoints to S3, or checks a bucket against the chain in read-only mode. | [charts/hyperlane-bullet-validator](charts/hyperlane-bullet-validator/README.md) |

To run a validator as a sealed, attestable EC2 image instead of on Kubernetes, see
[validators/hyperlane-bullet-validator](validators/hyperlane-bullet-validator/README.md).

## Installing

Charts are published as OCI artifacts to `oci://ghcr.io/bulletxyz/charts/<chart>`
(Helm 3.8 or later). There is no `helm repo add` step; always pin `--version`.

```sh
helm show values oci://ghcr.io/bulletxyz/charts/hyperlane-bullet-validator --version <chart-version>

helm upgrade --install validator \
  oci://ghcr.io/bulletxyz/charts/hyperlane-bullet-validator \
  --version <chart-version> \
  --namespace hyperlane --create-namespace \
  --values my-values.yaml
```

## Images

Images are published to `ghcr.io/bulletxyz/<image>`, independently of the chart
names. A chart version deploys the image tag matching its `appVersion` by default.

| Image | Deployed by | Platforms |
| ----- | ----------- | --------- |
| `ghcr.io/bulletxyz/hyperlane-bullet-validator:<appVersion>` | `hyperlane-bullet-validator` | `linux/amd64`, `linux/arm64` |

## Releases and verification

Every chart release has a git tag and a GitHub release named
`<chart>-<chart-version>` (none is marked "Latest"). The release carries the
image as an OCI layout archive named `<image>-<appVersion>.oci.tar`, and the
tagged commit records the versions, image name and image digest as git trailers:

```sh
git show -s --format='%(trailers)' hyperlane-bullet-validator-<chart-version>
```

Published chart versions and image tags are never overwritten. To check that an
image tag still resolves to the recorded digest, run
`crane digest ghcr.io/bulletxyz/hyperlane-bullet-validator:<appVersion>`; to pin
a deployment to it, set `image.digest`.

## Development

This repository is published automatically, so pull requests here are not
merged directly. To check a change locally, run `.github/scripts/lint_charts.sh`.

## License

[MIT](LICENSE)
