{
  description = "Hyperlane checkpoint validator for the Bullet rollup as a sealed, attestable EC2 image";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    nitro-tee.url = "github:aws/nitrotpm-attestation-samples?dir=nix";
    nitro-tee.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    {
      self,
      nixpkgs,
      nitro-tee,
      ...
    }:
    let
      system = "aarch64-linux";
      pkgs = nixpkgs.legacyPackages.${system};

      # What the validator signs. The image measures these, so the attestation
      # covers them along with the code.
      sequencerUrl = "https://rollup.mainnet.bullet.xyz";
      mailboxDomain = "5555";
      # Where publish-ami registers the image.
      regions = "ap-northeast-1 eu-north-1 eu-west-1";

      # The release export stamps the published image's digest into the chart,
      # so the measurement covers the validator code too.
      chart = builtins.readFile ../../charts/hyperlane-bullet-validator/Chart.yaml;
      imageDigest = builtins.head (
        builtins.elemAt (builtins.split "\n  bullet\\.xyz/image-digest: \"(sha256:[0-9a-f]{64})\"" chart) 1
      );

      # The key lives in a TPM NV index readable only under this image's boot
      # measurements: PCR4 (the UKI, which pins the verity store) and PCR12
      # (its command line, unmodified). verify.py checks the same two.
      nvIndex = "0x01000100";
      pcrs = "sha384:4,12";

      validator =
        { pkgs, ... }:
        {
          system.stateVersion = "26.05";
          # The serial console, read with `aws ec2 get-console-output`, is the
          # only window into the box.
          services.journald.console = "/dev/ttyS0";

          systemd.services.validator-key = {
            wants = [ "network-online.target" ];
            after = [ "network-online.target" ];
            path = with pkgs; [
              awscli2
              curl
              nitrotpm-tools
              openssl
              tpm2-tools
              xxd
            ];
            environment.TPM2TOOLS_TCTI = "device:/dev/tpmrm0";
            serviceConfig = {
              Type = "oneshot";
              RemainAfterExit = true;
              RuntimeDirectory = "validator";
              RuntimeDirectoryMode = "0700";
            };
            script = ''
              set -o pipefail
              cd /run/validator
              token=$(curl -sf -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)
              imds() { curl -sf -H "X-aws-ec2-metadata-token: $token" "http://169.254.169.254/latest/$1"; }
              region=$(imds meta-data/placement/region)
              # Nothing measures user data, so it may only say where signatures go.
              [[ $(imds user-data) =~ ^s3://([a-z0-9.-]+)/([A-Za-z0-9._/-]+)$ ]] ||
                { echo "user data must be s3://<bucket>/<prefix>" >&2; exit 1; }
              bucket=''${BASH_REMATCH[1]} prefix=''${BASH_REMATCH[2]}
              # key.der is SEC1 DER: the private key is bytes 7..39, the public key the last 64.
              address() { tail -c 64 key.der | openssl dgst -keccak-256 -binary | tail -c 20; }

              if ! tpm2_nvreadpublic ${nvIndex} >/dev/null 2>&1; then
                openssl ecparam -name secp256k1 -genkey -noout -outform DER -out key.der
                address > address.bin
                # Attest only a key born here: one found in the TPM may have been planted.
                nitro-tpm-attest --user-data address.bin > attestation.cbor
                until aws s3 cp --region "$region" attestation.cbor "s3://$bucket/$prefix/0x$(xxd -p address.bin)/attestation.cbor"; do
                  sleep 10
                done
                tpm2_createpolicy --policy-pcr -l ${pcrs} -L pcr.policy
                tpm2_nvdefine ${nvIndex} -C o -s 118 -L pcr.policy -a "policyread|policywrite"
                tpm2_nvwrite ${nvIndex} -P pcr:${pcrs} -i key.der
              fi
              tpm2_nvread ${nvIndex} -P pcr:${pcrs} -s 118 -o key.der
              cat > validator.env <<EOF
              HYPERLANE_VALIDATOR_VALIDATOR_KEY=$(head -c 39 key.der | tail -c 32 | xxd -p -c 32)
              HYPERLANE_VALIDATOR_S3_BUCKET=$bucket
              HYPERLANE_VALIDATOR_S3_REGION=$region
              HYPERLANE_VALIDATOR_S3_PREFIX=$prefix/0x$(address | xxd -p)
              EOF
              rm key.der
            '';
          };

          virtualisation.oci-containers.containers.hyperlane-bullet-validator = {
            image = "ghcr.io/bulletxyz/hyperlane-bullet-validator@${imageDigest}";
            environment = {
              HYPERLANE_VALIDATOR_SEQUENCER_URL = sequencerUrl;
              HYPERLANE_VALIDATOR_MAILBOX_DOMAIN = mailboxDomain;
            };
            environmentFiles = [ "/run/validator/validator.env" ];
            # The instance role's credentials come from IMDS, a single hop away.
            extraOptions = [ "--network=host" ];
          };
          systemd.services.podman-hyperlane-bullet-validator = {
            requires = [ "validator-key.service" ];
            after = [ "validator-key.service" ];
            # Paces a crash loop (e.g. a history divergence) under systemd's start limit.
            serviceConfig.RestartSec = 10;
          };
        };

      image = nitro-tee.lib.${system}.tee-image { userConfig = validator; };

      app = name: runtimeInputs: text: {
        type = "app";
        program = "${pkgs.writeShellApplication { inherit name runtimeInputs text; }}/bin/${name}";
      };
    in
    {
      packages.${system}.default = image;

      apps.${system} = {
        # `publish-ami <version>` registers the image as the public AMI
        # hyperlane-bullet-validator-<version> in each region. Safe to rerun.
        publish-ami = app "publish-ami" [ pkgs.awscli2 pkgs.coldsnap ] ''
          name=hyperlane-bullet-validator-$1
          for region in ${regions}; do
            export AWS_REGION=$region
            ami=$(aws ec2 describe-images --owners self --filters "Name=name,Values=$name" --query 'Images[0].ImageId' --output text)
            if [ "$ami" = None ]; then
              snapshot=$(coldsnap upload --wait ${image}/*.raw)
              ami=$(aws ec2 register-image --name "$name" --architecture arm64 --boot-mode uefi --tpm-support v2.0 \
                --ena-support --virtualization-type hvm --root-device-name /dev/xvda \
                --block-device-mappings "DeviceName=/dev/xvda,Ebs={SnapshotId=$snapshot}" --query ImageId --output text)
            fi
            aws ec2 wait image-available --image-ids "$ami"
            aws ec2 modify-image-attribute --image-id "$ami" --launch-permission 'Add=[{Group=all}]'
            echo "$region $ami"
          done
          cat ${image}/tpm_pcr.json
        '';

        verify =
          let
            check = pkgs.writers.writePython3 "verify" {
              libraries = with pkgs.python3Packages; [
                cbor2
                cryptography
              ];
              flakeIgnore = [ "E501" ];
            } (builtins.readFile ./verify.py);
          in
          app "verify" [ ] ''exec ${check} ${image}/tpm_pcr.json "$@"'';
      };
    };
}
