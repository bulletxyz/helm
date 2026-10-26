# hyperlane-bullet-validator

A Helm chart for running a [Hyperlane](https://hyperlane.xyz) checkpoint
validator for the Bullet rollup.

The validator follows the rollup's merkle-tree-hook. For every message
inserted into the tree it signs a checkpoint (EIP-191 over the Hyperlane
checkpoint digest) and uploads it to an S3 bucket, where relayers pick it up.
It keeps no state on disk: on startup it reconciles the bucket against the
chain and signs any missing checkpoint, then polls for new insertions.

- [Modes](#modes)
- [Prerequisites](#prerequisites)
- [Installing](#installing)
- [Network settings](#network-settings)
- [Examples](#examples)
- [Validator key handling](#validator-key-handling)
- [Announcing the validator](#announcing-the-validator)
- [Values](#values)
- [Metrics and health](#metrics-and-health)
- [Upgrading and versioning](#upgrading-and-versioning)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)

## Modes

`validator.mode` is required and has no default.

| Mode       | Key              | AWS credentials             | What it does                                                                                                                                                                          |
| ---------- | ---------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `signer`   | required         | `s3:PutObject` on the prefix | Signs every checkpoint and uploads it to `s3://<bucket>/<prefix>/`.                                                                                                                   |
| `readonly` | must not be set  | none                        | Demo mode. It runs the same pipeline against the bucket, but instead of signing a missing checkpoint it logs an error. It checks the newest checkpoint's root, index and message id against the chain, but not its signature. It never writes anything and uses only public reads, provided `extraEnvFrom` does not inject `HYPERLANE_VALIDATOR_*` variables. |

Read-only mode is a safe way to try the chart against Bullet's own public
bucket (see [Network settings](#network-settings)). Only signer mode verifies
signatures, by comparing the newest checkpoint's signer with its own key.

The chart refuses to render a `signer` without a key, a `readonly` install with
a key, or an install that leaves any required network setting unset, so the validator
never falls back to the mainnet configuration built into the binary.

## Prerequisites

- Kubernetes on `linux/amd64` or `linux/arm64` nodes. The image is a
  multi-arch index for both.
- Helm 3.8 or later (for OCI registry support). CI tests the chart with Helm
  3.22.
- Outbound HTTPS to the rollup REST endpoint (`sequencerUrl`) and to
  `https://<bucket>.s3.<region>.amazonaws.com`.
- For `signer` mode, also:
  - a secp256k1 validator key,
  - an S3 bucket set up as described below,
  - AWS credentials that can write to it,
  - an on-chain announcement of the validator (see
    [Announcing the validator](#announcing-the-validator)).

### S3 bucket

The validator reads the bucket **anonymously over public HTTPS**
(`https://<bucket>.s3.<region>.amazonaws.com`), even in signer mode; only
writes go through the AWS SDK. Relayers read signatures the same way, so the
bucket must be publicly readable anyway.

| Who                | Permission                          | Resource                         | Why                                                                                                     |
| ------------------ | ----------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Everyone (`"*"`)   | `s3:GetObject`                      | `arn:aws:s3:::<bucket>/<prefix>/*` | Reads checkpoints and `checkpoint_latest_index.json`.                                                  |
| Everyone (`"*"`)   | `s3:ListBucket`                     | `arn:aws:s3:::<bucket>`          | Lists `<prefix>/checkpoint_*` to find the signed indices. This permission also makes S3 return 404, not 403, for a key that does not exist. |
| The validator only | `s3:PutObject`                      | `arn:aws:s3:::<bucket>/<prefix>/*` | Uploads checkpoints and `announcement.json` (signer mode only). The validator never overwrites or deletes a checkpoint. |

The validator treats `403 Forbidden` as a misconfiguration and stops the
pass, so without anonymous `s3:ListBucket` (S3 then answers 403 for missing
keys) it cannot start. Use a dedicated bucket, since anonymous `ListBucket`
exposes every key name in it. Also:

- Turn off the S3 Block Public Access settings that block public bucket
  policies (`BlockPublicPolicy` and `RestrictPublicBuckets`) on this bucket.
- Keep the default SSE-S3 encryption. Anonymous readers cannot read SSE-KMS
  encrypted objects.

Example bucket policy for the prefix `bulletmainnet`, following
[Hyperlane's validator bucket guide](https://docs.hyperlane.xyz/docs/operate/validators/validator-signatures-aws):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadCheckpoints",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::my-validator-bucket/bulletmainnet/*"
    },
    {
      "Sid": "PublicListBucket",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::my-validator-bucket"
    },
    {
      "Sid": "ValidatorWrites",
      "Effect": "Allow",
      "Principal": { "AWS": "arn:aws:iam::123456789012:role/my-validator" },
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::my-validator-bucket/bulletmainnet/*"
    }
  ]
}
```

### AWS credentials (signer mode)

Writes use the AWS SDK's default credential chain. Pick one of these:

- **IRSA (EKS IAM roles for service accounts).** Give a role `s3:PutObject` on
  the prefix, trust the cluster's OIDC provider for the chart's service
  account, and annotate the service account:

  ```yaml
  serviceAccount:
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/my-validator
  ```

- **EKS Pod Identity.** Install the EKS Pod Identity Agent add-on, then create
  a pod identity association for the release namespace and the chart's
  service account. The account is named `<release>-hyperlane-bullet-validator`
  by default; set `serviceAccount.name` to choose the name yourself. The chart
  itself needs no extra values.

- **Static credentials.** Put `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
  in a Secret and pass it through `extraEnvFrom`:

  ```sh
  kubectl create secret generic validator-aws-credentials \
    --from-literal=AWS_ACCESS_KEY_ID=... --from-literal=AWS_SECRET_ACCESS_KEY=...
  ```

  ```yaml
  extraEnvFrom:
    - secretRef:
        name: validator-aws-credentials
  ```

  Use a dedicated IAM user whose only permission is `s3:PutObject` on the
  prefix.

Read-only mode creates no S3 client and needs no credentials.

## Installing

The chart is published as `oci://ghcr.io/bulletxyz/charts/hyperlane-bullet-validator`
and its image as `ghcr.io/bulletxyz/hyperlane-bullet-validator`.

```sh
# List the chart's default values, or read its documentation.
helm show values oci://ghcr.io/bulletxyz/charts/hyperlane-bullet-validator --version <chart-version>
helm show readme oci://ghcr.io/bulletxyz/charts/hyperlane-bullet-validator --version <chart-version>

# Install (or upgrade) with your values file.
helm upgrade --install validator \
  oci://ghcr.io/bulletxyz/charts/hyperlane-bullet-validator \
  --version <chart-version> \
  --namespace hyperlane --create-namespace \
  --values my-values.yaml
```

Chart versions are listed on the
[releases page](https://github.com/bulletxyz/helm/releases) as
`hyperlane-bullet-validator-<chart-version>`. Always pass `--version`.

## Network settings

| Setting         | Bullet mainnet                       | Bullet testnet                       |
| --------------- | ------------------------------------ | ------------------------------------ |
| `mailboxDomain` | `5555`                               | `5556`                               |
| `sequencerUrl`  | `https://rollup.mainnet.bullet.xyz`  | `https://rollup.testnet.bullet.xyz`  |
| `wsUrl`         | leave empty                          | leave empty                          |

Bullet's own validator publishes to public buckets. You can check them
against the chain in read-only mode:

| Network | `s3.bucket`                          | `s3.region`      | `s3.prefix`                 |
| ------- | ------------------------------------ | ---------------- | --------------------------- |
| mainnet | `hyperlane-validator-mainnet-bucket` | `ap-northeast-1` | `bulletmainnet-signatures`  |
| testnet | `hyperlane-validator-testnet-bucket` | `ap-northeast-1` | `bullettestnet-signatures`  |

In signer mode, **never** point at Bullet's buckets. Use your own bucket and
prefix. Before signing, the validator checks that the newest checkpoint in the
bucket was signed by its own key, and it exits if not (see
[Troubleshooting](#troubleshooting)).

`wsUrl` is the rollup's ledger event websocket, used to wake up as soon as a
message is inserted. The public endpoints do not serve it, so leave it empty;
the validator then polls every `scanIntervalSecs` (30 seconds by default).

## Examples

### Read-only check of Bullet mainnet

No key and no AWS credentials:

```yaml
validator:
  mode: readonly
sequencerUrl: https://rollup.mainnet.bullet.xyz
mailboxDomain: 5555
s3:
  bucket: hyperlane-validator-mainnet-bucket
  region: ap-northeast-1
  prefix: bulletmainnet-signatures
```

The logs should show `Reconcile complete; following new insertions` and then
a periodic `Audit OK: rebuilt tree matches chain history`.

### Mainnet signer with an existing Secret and IRSA

```yaml
validator:
  mode: signer
  key:
    existingSecret: hyperlane-validator-key   # created out of band, see below
    existingSecretKey: validator-key
sequencerUrl: https://rollup.mainnet.bullet.xyz
mailboxDomain: 5555
s3:
  bucket: my-validator-bucket
  region: eu-west-1
  prefix: bulletmainnet
serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/my-validator
image:
  # Optional: pin the exact image (see Upgrading and versioning).
  digest: sha256:<index digest>
```

For testnet, use `mailboxDomain: 5556`, `sequencerUrl:
https://rollup.testnet.bullet.xyz`, and a different bucket or prefix with a
different key.

More examples are in the chart's `ci/` directory in the
[source repository](https://github.com/bulletxyz/helm).

## Validator key handling

The key is a secp256k1 private key: 32 bytes, hex encoded, with or without a
`0x` prefix. Its address is the validator's identity. Anyone holding the key
can sign checkpoints in your name.

- **Prefer `validator.key.existingSecret`.** Create the Secret out of band.
  Best is to sync it from a secret manager with
  [External Secrets Operator](https://external-secrets.io), for example from
  AWS Secrets Manager or Parameter Store:

  ```yaml
  apiVersion: external-secrets.io/v1
  kind: ExternalSecret
  metadata:
    name: hyperlane-validator-key
  spec:
    refreshInterval: 1h
    secretStoreRef:
      kind: ClusterSecretStore
      name: aws-parameter-store
    target:
      name: hyperlane-validator-key
    data:
      - secretKey: validator-key
        remoteRef:
          key: /my/validator/signer-key
  ```

  Use the `apiVersion` your External Secrets Operator release serves.

  For a quick start, `kubectl create secret generic hyperlane-validator-key
  --from-literal=validator-key=0x...` works too.

- **Avoid `validator.key.value`.** The chart then creates the Secret, but the
  key also ends up in your values file, your shell history (with `--set`) and
  the Helm release record. Use it only for throwaway test keys.

- Run **exactly one** validator per key. The chart fixes `replicaCount` at 1
  and uses the `Recreate` strategy, so two pods never sign at once.

- Restrict who can read Secrets in the release namespace, and enable
  encryption at rest for Secrets in the cluster.

- Changing `validator.key.value` restarts the pod, because the chart puts a
  checksum annotation on it. Changing an existing Secret does not. After a
  rotation, run `kubectl rollout restart deploy/<release>-hyperlane-bullet-validator`.
  A rotated key also needs a new announcement, and the bucket's history was
  signed by the old key. See [Troubleshooting](#troubleshooting).

## Announcing the validator

**This chart does not announce the validator on-chain.** A Hyperlane validator
must announce its signature location on-chain (ValidatorAnnounce); until then
relayers ignore its bucket. In signer mode the validator signs that
announcement with its key at startup and uploads it as
`<prefix>/announcement.json`. Bullet submits it, so the key never has to
leave the pod. Its signatures also only count once it is in the validator set
of the relevant multisig ISM. Coordinate both steps with Bullet.

## Values

| Key | Type | Default | Description |
| --- | ---- | ------- | ----------- |
| `validator.mode` | string | `""` | **Required.** `signer` or `readonly`. See [Modes](#modes). |
| `validator.key.existingSecret` | string | `""` | Existing Secret holding the validator key (recommended). Signer mode only. |
| `validator.key.existingSecretKey` | string | `"validator-key"` | Key within `existingSecret` that holds the private key. |
| `validator.key.value` | string | `""` | Inline private key (64 hex chars, optional `0x`). The chart creates a Secret from it. Mutually exclusive with `existingSecret`; not recommended. |
| `sequencerUrl` | string | `""` | **Required.** Rollup REST endpoint (`http(s)://...`). → `HYPERLANE_VALIDATOR_SEQUENCER_URL` |
| `wsUrl` | string | `""` | Optional ledger websocket base (`ws(s)://...`). Leave empty for public endpoints. → `HYPERLANE_VALIDATOR_WS_URL` (set only when non-empty) |
| `mailboxDomain` | integer | `0` | **Required.** Hyperlane domain id of the rollup: 5555 mainnet, 5556 testnet. `0` means unset. → `HYPERLANE_VALIDATOR_MAILBOX_DOMAIN` |
| `s3.bucket` | string | `""` | **Required.** Signature bucket. → `HYPERLANE_VALIDATOR_S3_BUCKET` |
| `s3.region` | string | `""` | **Required.** Bucket region. → `HYPERLANE_VALIDATOR_S3_REGION` |
| `s3.prefix` | string | `""` | **Required.** Key prefix inside the bucket. → `HYPERLANE_VALIDATOR_S3_PREFIX` |
| `scanIntervalSecs` | integer | `30` | Polling interval of the scan loop. It also scales the liveness staleness window (10 × interval). → `HYPERLANE_VALIDATOR_SCAN_INTERVAL_SECS` |
| `logLevel` | string | `"info"` | Log filter in `RUST_LOG` syntax, e.g. `info,hyperlane_bullet_validator=debug`. → `RUST_LOG` |
| `ports.metrics` | integer | `9000` | Prometheus metrics port. → `HYPERLANE_VALIDATOR_METRICS_PORT` |
| `ports.health` | integer | `8080` | Health port (`/health`, `/healthz`, `/ready`). Must differ from `ports.metrics`. → `HYPERLANE_VALIDATOR_HEALTH_PORT` |
| `startupProbe.periodSeconds` | integer | `10` | Seconds between startup probes of `/healthz`. |
| `startupProbe.failureThreshold` | integer | `30` | Failed startup probes before the container is restarted. The defaults allow 5 minutes. See [Metrics and health](#metrics-and-health). |
| `image.repository` | string | `"ghcr.io/bulletxyz/hyperlane-bullet-validator"` | Image repository. |
| `image.tag` | string | `""` | Image tag. Defaults to the chart's `appVersion`. |
| `image.digest` | string | `""` | Image digest (`sha256:...`). Takes precedence over `image.tag`. |
| `image.pullPolicy` | string | `"IfNotPresent"` | Image pull policy. |
| `imagePullSecrets` | list | `[]` | Pull secrets, for a private mirror. The GHCR image is public. |
| `replicaCount` | integer | `1` | Fixed at 1; any other value is rejected. One signer per key. |
| `serviceAccount.create` | bool | `true` | Create a ServiceAccount. |
| `serviceAccount.name` | string | `""` | ServiceAccount name. Defaults to the full name if created, otherwise to `default`. |
| `serviceAccount.annotations` | object | `{}` | ServiceAccount annotations, e.g. `eks.amazonaws.com/role-arn` for IRSA. |
| `extraEnv` | list | `[]` | Extra container environment variables, e.g. `AWS_*` settings. Names starting with `HYPERLANE_VALIDATOR_`, and `RUST_LOG`, are rejected because the chart manages them. |
| `extraEnvFrom` | list | `[]` | Extra `envFrom` sources, e.g. a Secret with static AWS credentials. These are not checked: do not put `HYPERLANE_VALIDATOR_*` variables in them. |
| `prometheusScrapeAnnotations` | bool | `true` | Add `prometheus.io/scrape`, `prometheus.io/port` and `prometheus.io/path` annotations to the pod. |
| `podAnnotations` | object | `{}` | Extra pod annotations. Annotations the chart sets take precedence. |
| `podLabels` | object | `{}` | Extra pod labels. The selector labels `app.kubernetes.io/name` and `app.kubernetes.io/instance` are rejected, because the Deployment and Service select pods by them. |
| `podSecurityContext` | object | non-root 65532, `fsGroup` 65532, `RuntimeDefault` seccomp | Pod security context. |
| `securityContext` | object | non-root 65532, read-only root filesystem, no privilege escalation, all capabilities dropped, `RuntimeDefault` seccomp | Container security context. The validator writes no files, so it works with a read-only root filesystem. |
| `resources` | object | requests `100m`/`128Mi`, limits `512m`/`256Mi` | Container resources. |
| `nodeSelector` | object | `{}` | Node selector. |
| `tolerations` | list | `[]` | Tolerations. |
| `affinity` | object | `{}` | Affinity. |
| `priorityClassName` | string | `""` | Priority class. |
| `service.enabled` | bool | `true` | Create a ClusterIP Service for the `metrics` and `health` ports. |
| `service.annotations` | object | `{}` | Service annotations. |
| `serviceMonitor.enabled` | bool | `false` | Create a Prometheus Operator ServiceMonitor. It is rendered only when the cluster serves `monitoring.coreos.com/v1`, and it requires `service.enabled`. |
| `serviceMonitor.interval` | string | `"30s"` | Scrape interval. |
| `serviceMonitor.scrapeTimeout` | string | `""` | Scrape timeout. Empty means the Prometheus default. |
| `serviceMonitor.labels` | object | `{}` | Extra ServiceMonitor labels, e.g. the label your Prometheus selects on. |
| `serviceMonitor.annotations` | object | `{}` | ServiceMonitor annotations. |
| `serviceMonitor.relabelings` | list | `[]` | Relabelings. |
| `serviceMonitor.metricRelabelings` | list | `[]` | Metric relabelings. |
| `nameOverride` | string | `""` | Override the chart name in resource names and labels. |
| `fullnameOverride` | string | `""` | Override the full resource name. |

`values.schema.json` rejects unknown keys and malformed values (digest, URL
scheme, key format), so typos fail early.

## Metrics and health

The health port serves:

| Path       | Meaning                                                                                                                                                       | Used by         |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| `/ready`   | 200 when a full pass has completed within the last `10 × scanIntervalSecs`. A daemon that keeps retrying against a dead dependency reports not ready, but stays live. | readiness probe |
| `/health`  | 200 while the daemon is making progress. Unhealthy after `10 × scanIntervalSecs` without progress, with a 2-minute startup grace period.                        | liveness probe  |
| `/healthz` | Always 200 while the process serves HTTP.                                                                                                                      | startup probe   |

The health server starts only after the S3 client is set up and, in signer
mode, the announcement has been uploaded and checked. The startup probe holds back the
other probes until `/healthz` answers, and restarts the container if it does
not within `startupProbe.periodSeconds × startupProbe.failureThreshold`
(5 minutes by default).

The metrics port serves Prometheus metrics at `/metrics`. Names are shown here
as recorded in the code; the exporter replaces the dots with underscores.

| Metric | Type | Meaning |
| ------ | ---- | ------- |
| `hyperlane_validator.signed` | counter | Checkpoints signed and uploaded. |
| `hyperlane_validator.latest_signed_index` | gauge | Index of the latest checkpoint signed by this process. |
| `hyperlane_validator.reconcile.missing` | gauge | Checkpoints the startup reconcile found missing and signed (or, in read-only mode, reported). |
| `hyperlane_validator.reconcile.errors` | counter | Failed startup reconcile attempts, which are retried. |
| `hyperlane_validator.scan.errors` | counter | Failed follow passes, which are retried. |
| `hyperlane_validator.demo.would_sign` | counter | Read-only mode: checkpoints the validator would have signed. |
| `hyperlane_validator.ws.*` | counters | Websocket connects, events, disconnects and connect errors (only with `wsUrl`). |

Suggested alerts:

- the pod is restarting (see divergence below);
- `/ready` failing for longer than a few scan intervals;
- `scan.errors` increasing steadily;
- in read-only mode, `demo.would_sign` increasing, which means the checked
  bucket is missing checkpoints.

## Upgrading and versioning

- **Chart version** (`version`) follows semver, independently of the image,
  and is immutable once published.
- **App version** (`appVersion`) is the image tag the chart deploys by
  default. One app version always maps to one image digest.
- Each chart release has a git tag and GitHub release
  `hyperlane-bullet-validator-<chart-version>` that attaches the image as
  `hyperlane-bullet-validator-<appVersion>.oci.tar`; its commit records
  `Chart-Version`, `App-Version`, `Image-Name` and `Image-Digest` trailers.

To pin the image exactly, set `image.digest` to the release's `Image-Digest`,
which you can check against the registry:

```sh
crane digest ghcr.io/bulletxyz/hyperlane-bullet-validator:<appVersion>
# or
docker buildx imagetools inspect ghcr.io/bulletxyz/hyperlane-bullet-validator:<appVersion>
```

To upgrade, run `helm upgrade` with the new `--version`. With the `Recreate`
strategy there is a short gap in signing, which the new pod reconciles. Read
the release notes before you cross a major chart version.

## Troubleshooting

**The pod crash-loops with `HISTORY DIVERGENCE: ...`.** This is intentional.
The validator audits the newest checkpoint in the bucket against the chain
before every signing batch. If they disagree, it exits without writing
anything, and the crash loop is the alert. Typical causes:

- `stored object was signed by 0x... but this daemon's validator key is 0x...`:
  the bucket or prefix belongs to another key, or the key was rotated. Point
  the validator at the right bucket and prefix, or start a fresh prefix for a
  new key, which then needs a new announcement.
- `... chain rollback / reset` or a root mismatch: the bucket does not match
  the chain behind `sequencerUrl`. This can mean the wrong network (check
  `mailboxDomain`, `sequencerUrl` and the bucket against each other), or a
  rollup reset. Recovery is a human decision. Do not delete objects from the
  bucket until you know why they diverge.

**`GET ...: 403 Forbidden — bucket or policy misconfigured`.** Anonymous reads
are not allowed. Check the bucket policy and the Block Public Access settings
in [S3 bucket](#s3-bucket).

**Upload errors (`PUT ...`) in signer mode.** The pod has no AWS credentials,
or they lack `s3:PutObject` on `<bucket>/<prefix>/*`. With IRSA, check the
service account annotation and the role's trust policy. With Pod Identity,
check the association's namespace and service account name.

**`... not configured (HYPERLANE_VALIDATOR_...)` at startup.** A setting
reached the binary empty. Inspect the rendered Deployment
(`kubectl get deploy -o yaml`) for the variable and for any admission webhook
that mutated the pod.

**`/ready` never turns green.** The first reconcile has not finished. Check the
logs for repeated `reconcile` errors: the rollup endpoint may be unreachable,
or the bucket reads may be failing.

## Uninstalling

```sh
helm uninstall validator --namespace hyperlane
```

The bucket and its checkpoints are left untouched, and so is any Secret you
created yourself.
