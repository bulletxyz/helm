"""Check a validator's NitroTPM attestation document against this build and
print the validator address it attests.

Usage: verify <tpm_pcr.json> <attestation.cbor>
"""

import datetime
import hashlib
import json
import sys

import cbor2
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

# SHA-256 of AWS_NitroEnclaves_Root-G1, which also roots NitroTPM documents.
ROOT_SHA256 = "641a0321a3e244efe456463195d606317ed7cdcc3c1756e09893f3c68f79bb5b"


def check(ok, message):
    if not ok:
        sys.exit(f"verify: {message}")


expected = json.load(open(sys.argv[1]))["Measurements"]
cose = cbor2.load(open(sys.argv[2], "rb"))
# COSE_Sign1, tagged or not.
protected, _, payload, signature = getattr(cose, "value", cose)
doc = cbor2.loads(payload)

check(hashlib.sha256(doc["cabundle"][0]).hexdigest() == ROOT_SHA256, "not rooted at the AWS Nitro root")
chain = [x509.load_der_x509_certificate(c) for c in doc["cabundle"] + [doc["certificate"]]]
for issuer, cert in zip(chain, chain[1:]):
    cert.verify_directly_issued_by(issuer)
# The leaf lives for hours, so validity is checked when the document was made.
made = datetime.datetime.fromtimestamp(doc["timestamp"] / 1000, datetime.timezone.utc)
check(all(c.not_valid_before_utc <= made <= c.not_valid_after_utc for c in chain), "certificate not valid at the document's timestamp")
r, s = int.from_bytes(signature[:48], "big"), int.from_bytes(signature[48:], "big")
chain[-1].public_key().verify(
    encode_dss_signature(r, s),
    cbor2.dumps(["Signature1", protected, b"", payload]),
    ec.ECDSA(hashes.SHA384()),
)

for pcr in (4, 12):
    check(doc["nitrotpm_pcrs"][pcr].hex() == expected[f"PCR{pcr}"], f"PCR{pcr} does not match this build")
check(len(doc.get("user_data") or b"") == 20, "user_data is not a validator address")
print("0x" + doc["user_data"].hex())
