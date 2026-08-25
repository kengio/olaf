# Asset provenance

This record identifies the public artwork shipped with OLAF without publishing
personal attribution or private working files.

## Public masters

| File | SHA-256 | Dimensions |
|---|---|---|
| `olaf-logo.png` | `68f46d4054cce80b23f5c8255f5f0c2c306da116adbbe3c094ca0ec0d78efa28` | 512 × 512 px |
| `olaf-mascot.png` | `3078c87bd9d9a74030fb73df034f17380d01387a8dde29edf6dca5aa19740f9c` | 512 × 512 px |
| `olaf-lockup-light.png` | `3bb7dc420e58bae403d267277f98ad1807cf859e48c30a01b3d1780899fdb887` | 1200 × 600 px |
| `olaf-lockup-dark.png` | `0a8dcda65273cc73a079cc7b068a046dd9010c65ea50057f1dca42d84a4c0b33` | 1200 × 600 px |
| `olaf-social-preview.png` | `f7cb6da4018700fccc49e59185d329b7491c0c5512bb50e7fc2a68306c07e9db` | 1200 × 630 px |

## Origin and transformations

The arctic-owl logo, mascot, and lockups were created for OLAF by project
contributors using generative-image tools and contributor-directed cleanup. No
customer material, personal likeness, private project asset, stock image, third-party
logo, Microsoft logo, or Microsoft product icon was supplied as source material.

The social-preview image is a deterministic composition of `olaf-logo.png` centered
on the project's Frost/Ice background. It contains no text, embedded profile, or
personal metadata and introduces no third-party mark.

The lockups contain the owner-selected project name “OLAF — OneLake Access
Framework.” This referential product-name use carries the trademark risk documented
in the [brand guidelines](../../docs/brand-guidelines.md#legal-separation); the
independence disclaimer is not a trademark license.

## License

The repository's [MIT license](../../LICENSE) applies to these files. The usage
guidance in [`docs/brand-guidelines.md`](../../docs/brand-guidelines.md) is a request,
not an additional license condition.

Verify a master from the repository root with:

```bash
shasum -a 256 assets/brand/<file>
```
