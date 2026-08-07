# Third-party 3D assets

## `kestrel-drone.glb`

The hero aircraft on the landing page — a DJI Matrice 300 RTK, chosen because it is
the class of airframe KESTREL is written for rather than a generic quadcopter.

| | |
|---|---|
| Source file | `quadcopter_dji_matrice_300_rtk.glb` |
| Original size | 41.86 MB (glTF 2.0, 46 meshes, 34 materials, **zero textures**) |
| Shipped size | 1.99 MB — a 22× reduction |
| Licence | **To be confirmed by the submitter before publication.** |

### How it was compressed

All 41.86 MB was raw vertex data, which makes Draco close to a best case. Texture
compression and simplification are both explicitly disabled, and so are `join` and
`flatten` — the default `optimize` pipeline merges the 46 meshes down to 6 nodes and
destroys the node names the landing page depends on (`Glass_Camera_*`,
`Glass_sensor`, `Yellow_lamp` are driven as emissive materials during the scroll
story).

```bash
npx @gltf-transform/cli@4 optimize <source>.glb web/public/models/kestrel-drone.glb \
    --compress draco \
    --texture-compress false \
    --simplify false \
    --join false \
    --flatten false
```

Verified afterwards: 51 nodes, 46 meshes, 6 materials, and all 8 lens/lamp targets
still addressable by name.

### Note on licence

Every other asset in this repository has its provenance stated — the footage is
Intel `sample-videos` under CC BY 4.0, and VisDrone was rejected specifically
because CC BY-NC-SA is incompatible with this use. This model was supplied for the
build and its origin was not recorded, so the same standard has not yet been met for
it. Before this repository is published or the demo is distributed, either fill in
the source and licence above, or replace the asset with one whose terms are known.

## `../draco/`

Draco decoder, copied from `three/examples/jsm/libs/draco/` (Apache 2.0). Only the
glTF variant is kept, since that is the only one `DRACOLoader` is pointed at. It is
served locally rather than from a CDN because the rest of KESTREL runs with no
network, and the landing page should hold to the same promise.
