/**
 * A minimal USTAR writer for kernel-built archives (export packages): regular
 * files only, fixed metadata, so the same entries always give the same bytes.
 */
export function tarArchive(entries: { name: string; data: Buffer }[]): Buffer {
  const parts: Buffer[] = []
  for (const { name, data } of [...entries].sort((a, b) => (a.name < b.name ? -1 : 1))) {
    if (Buffer.byteLength(name) > 99) throw new Error(`tar entry name too long: ${name}`)
    const header = Buffer.alloc(512)
    header.write(name, 0, 100, 'utf8')
    header.write('0000644\0', 100, 8, 'ascii')
    header.write('0000000\0', 108, 8, 'ascii')
    header.write('0000000\0', 116, 8, 'ascii')
    header.write(data.length.toString(8).padStart(11, '0') + '\0', 124, 12, 'ascii')
    header.write('00000000000\0', 136, 12, 'ascii')
    header.write('        ', 148, 8, 'ascii')
    header.write('0', 156, 1, 'ascii')
    header.write('ustar\0', 257, 6, 'ascii')
    header.write('00', 263, 2, 'ascii')
    const sum = header.reduce((a, b) => a + b, 0)
    header.write(sum.toString(8).padStart(6, '0') + '\0 ', 148, 8, 'ascii')
    parts.push(header, data, Buffer.alloc((512 - (data.length % 512)) % 512))
  }
  parts.push(Buffer.alloc(1024))
  return Buffer.concat(parts)
}
