# hyperlane-bullet-validator as a sealed EC2 image

[`flake.nix`](flake.nix) builds an
[Attestable AMI](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/attestable-ami.html)
that runs the [Hyperlane validator](../../charts/hyperlane-bullet-validator/README.md).
Bullet publishes the image as a public AMI for every release, and the
[Terraform module](terraform/main.tf) launches it. Nobody can log in to an
instance launched from it. The instance generates its validator key at first
boot and seals it into its NitroTPM, so no person ever holds the key. It then
publishes an attestation document, signed by AWS, which proves that the key was
generated inside exactly this image. Anyone can check that document against a
rebuild of the same flake, and Bullet does so before trusting the validator. To
update, you launch a new instance, which gets a new key.

- [How it works](#how-it-works)
- [Deploying](#deploying)
- [Verifying](#verifying)
- [Updating](#updating)
- [Checking on the validator](#checking-on-the-validator)

## How it works

- **The image is read-only and reproducible.** It is built with
  [nitrotpm-attestation-samples](https://github.com/aws/nitrotpm-attestation-samples/tree/main/nix)
  and pinned in `flake.lock`. The instance boots a single kernel image whose
  command line carries the dm-verity root hash of the Nix store. The NitroTPM
  measures that kernel image into PCR4. PCR12 stays zero as long as nobody
  changes the command line. Changing any byte of the image changes PCR4. The
  image builds in what the validator signs: the sequencer URL and the mailbox
  domain, and the digest of the validator container, which the release stamps
  into the chart. The instance pulls the container by that digest at startup,
  so the measurement covers the code that holds the key.
- **There is no way in.** The image runs no SSH server, SSM agent or console
  login. The firewall accepts no inbound traffic. The only input is the
  instance's user data, `s3://<bucket>/<prefix>`, which says where signatures
  go but not what is signed.
- **On first boot** the instance:
  1. generates a secp256k1 key in RAM;
  2. requests an attestation document with the validator address as user
     data, and uploads it to `<prefix>/<address>/attestation.cbor`;
  3. only then stores the key in a NitroTPM NV index that can be read only
     under the same PCR4 and PCR12.
- **On every boot** the instance reads the key back from the TPM and starts the
  validator. The validator signs checkpoints into `<prefix>/<address>/` and
  uploads its `announcement.json` there. Every key therefore gets a fresh
  history.
- **The key's lifetime.** Reboots keep the key, and it is destroyed when the
  instance is terminated. AWS notes that stopping and starting an instance
  launched from an attestable AMI can change its measurements. If that
  happens, the key stays sealed and you replace the instance as you would for
  an update.

## Deploying

Bullet publishes the AMI in `ap-northeast-1` (Tokyo), `eu-north-1`
(Stockholm) and `eu-west-1` (Dublin).

1. Set up the S3 bucket in one of those regions as described under
   [S3 bucket](../../charts/hyperlane-bullet-validator/README.md#s3-bucket).
   The module gives the instance its write access, so the bucket needs only
   the public read statements.
2. Launch the validator in the bucket's region with the module. It creates the
   instance, a role that may only `s3:PutObject` under `<prefix>/`, and a
   security group without inbound rules.

   ```hcl
   module "validator" {
     source = "github.com/bulletxyz/helm//validators/hyperlane-bullet-validator/terraform?ref=hyperlane-bullet-validator-<chart-version>"

     versions  = ["<chart-version>"]
     bucket    = "my-validator-bucket"
     prefix    = "bulletmainnet"
     subnet_id = "<subnet with outbound internet access>"
   }
   ```

   The default `c7g.medium` is the smallest instance type known to produce
   NitroTPM attestation documents; T4g instances
   [cannot](https://github.com/aws/NitroTPM-Tools/issues/7). Any other type
   must also be Graviton (arm64).

3. Within a few minutes the bucket shows the validator's address:

   ```sh
   aws s3 ls s3://<bucket>/<prefix>/
   ```

4. Send Bullet the release and the address.

## Verifying

Verification needs the release's checkout and an aarch64 Linux machine with
Nix (flakes enabled):

```sh
git clone --branch hyperlane-bullet-validator-<chart-version> https://github.com/bulletxyz/helm
cd helm/validators/hyperlane-bullet-validator
curl -sO https://<bucket>.s3.<region>.amazonaws.com/<prefix>/<address>/attestation.cbor
nix run .#verify -- attestation.cbor
```

`verify` rebuilds the image and checks two things about the document. First,
that it is signed through AWS's Nitro root certificate. Second, that its PCR4
and PCR12 match the build. It then prints the attested address, which must
equal `<address>`. Bullet then submits the validator's announcement and adds
the address to the multisig ISM's validator set.

## Updating

Add the new release to `versions` next to the running one, so the old
validator keeps signing until Bullet trusts the new address. Then remove the
old release: Terraform terminates that instance, and its key is destroyed with
it.

## Checking on the validator

There is no shell. The serial console carries the boot and validator logs (the
instance ID is in the module's `instance_ids` output), and the bucket shows
progress:

```sh
aws ec2 get-console-output --instance-id <id> --latest --output text
curl -s https://<bucket>.s3.<region>.amazonaws.com/<prefix>/<address>/checkpoint_latest_index.json
```

A healthy signer logs `Signing checkpoints` with its address, then
`Reconcile complete; following new insertions` and, periodically,
`Audit OK: rebuilt tree matches chain history`. A service that keeps
restarting with `HISTORY DIVERGENCE` stops on purpose. This and S3 errors are
explained under
[Troubleshooting](../../charts/hyperlane-bullet-validator/README.md#troubleshooting).
