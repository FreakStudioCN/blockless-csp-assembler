# Third-party notices

Blockless PCB Studio is licensed under Apache-2.0. The following bundled third-party works retain their original licenses.

## Adafruit CAD Parts

The cached STEP, STL, 3MF, JPG, and GIF module assets under `public/assets/real-cad/` come from [Adafruit CAD Parts](https://github.com/adafruit/Adafruit_CAD_Parts). The source mapping and download URLs are recorded by `scripts/harvest-real-cad.mjs`; the checked-in manifest identifies the source repository.

MIT License

Copyright (c) 2016 Adafruit Industries

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## KiCadRoutingTools

`tools/KiCadRoutingTools-0.19.3/` is a pinned snapshot of [KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools). Its own `LICENSE` file is included in that directory.

MIT License

Copyright (c) 2026 drandyhaas

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Installed dependencies

JavaScript dependencies are not committed to this repository. Their package names, versions, integrity hashes, and declared licenses are recorded in `package-lock.json` and installed locally with `npm ci`.

KiCadRoutingTools Python dependencies are also not committed. `npm run kicad:setup-router` installs them into an ignored local directory from the pinned requirements file.
