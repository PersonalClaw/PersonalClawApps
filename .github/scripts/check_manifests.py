"""Every ``app.json`` parses against core's own manifest parser, and every app ships its licence.

Core's ``AppManifest`` is the real contract, not a hand-rolled schema here, so a manifest core
would reject fails CI. Each manifest must also round-trip stably (the parity apps rely on) and
pass ``validate()``. Every published bundle ships its own ``LICENSE``, because a bundle is
installed standalone and the repository-root file does not travel with it; and a manifest that
declares a licence must agree with the file it ships (only known first lines are mapped, and an
unrecognised file is not guessed at).

The one static check that needs core installed. Its row is ``manifest-validate``, the name the
docs give it.

    python .github/scripts/check_manifests.py
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def main() -> int:
    from personalclaw.sdk.manifest import AppManifest

    manifests = sorted(ROOT.glob("*/app.json"))
    bad = []
    for path in manifests:
        mf = path.relative_to(ROOT)
        try:
            m = AppManifest.from_dict(json.loads(path.read_text()))
            # round-trip must be stable (the parity contract apps rely on)
            assert AppManifest.from_dict(m.to_dict()).to_dict() == m.to_dict()
            errs = m.validate()
            if errs:
                bad.append(f"{mf}: " + "; ".join(errs))
        except Exception as e:  # noqa: BLE001
            bad.append(f"{mf}: {type(e).__name__}: {e}")
        # every published bundle ships its own LICENSE — a bundle is
        # installed standalone, so the repo-root file does not travel with it
        lic = path.parent / "LICENSE"
        if not lic.exists():
            bad.append(f"{mf.parent}: missing LICENSE (copy the repo-root MIT file)")
            continue
        # a manifest that DECLARES a license must agree with the file it ships —
        # a declared MIT-0 over an MIT file tells installers the wrong terms.
        # Only known first-lines are mapped; an unrecognized file is not guessed at.
        declared = json.loads(path.read_text()).get("license")
        first = lic.read_text().splitlines()[0].strip() if lic.read_text().strip() else ""
        actual = {"MIT License": "MIT", "MIT No Attribution": "MIT-0"}.get(first)
        if declared and actual and declared != actual:
            bad.append(f"{mf}: declares license {declared} but LICENSE file is {actual}")
    if bad:
        print("\n".join(bad))
        return 1
    print(f"OK: {len(manifests)} manifests valid + licensed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
