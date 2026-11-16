"""The client's store-only ZIP writer (apps/web/src/zip.ts), run in Node from the compiled client and read back by
Python's zipfile: names (UTF-8 too), bytes and every CRC-32 as written, nothing compressed."""
import base64
import json
import shutil
import subprocess
import zipfile
import io


def zipped(folder, files):
    script = (f"const z=await import({json.dumps((folder / 'zip.js').as_uri())});"
              "const files=JSON.parse(process.argv[1]).map(([name,b64])=>({name,data:new Uint8Array(Buffer.from(b64,'base64'))}));"
              "process.stdout.write(JSON.stringify({zip:Buffer.from(z.zipStore(files)).toString('base64'),"
              "crc:z.crc32(new TextEncoder().encode('123456789'))}));")
    out = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script,
                          json.dumps([[n, base64.b64encode(d).decode()] for n, d in files])],
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def test_zip_store_reads_back_with_every_crc(client_modules):
    files = [('Week page 1.png', b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 40),
             ('Clinic é page 2.png', b'second'), ('empty.png', b'')]
    out = zipped(client_modules, files)
    assert out['crc'] == 0xCBF43926  # the CRC-32 check value
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(out['zip']))) as z:
        assert z.testzip() is None
        assert [(i.filename, i.compress_type) for i in z.infolist()] == [(n, zipfile.ZIP_STORED) for n, _ in files]
        assert [z.read(n) for n, _ in files] == [d for _, d in files]
