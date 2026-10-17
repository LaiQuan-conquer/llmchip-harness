import { randomBytes } from 'node:crypto'

const ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'

let lastTime = -1
let lastRandom = new Uint8Array(10)

/** Monotonic ULID: 48-bit millisecond time + 80 random bits, Crockford base32. */
export function ulid(now = Date.now()): string {
  if (now <= lastTime) {
    // Same (or earlier) millisecond: increment the random part to stay sortable.
    now = lastTime
    for (let i = lastRandom.length - 1; i >= 0; i--) {
      if (lastRandom[i] !== 0xff) {
        lastRandom[i]!++
        break
      }
      lastRandom[i] = 0
    }
  } else {
    lastTime = now
    lastRandom = new Uint8Array(randomBytes(10))
  }
  let time = ''
  let t = now
  for (let i = 0; i < 10; i++) {
    time = ALPHABET[t % 32]! + time
    t = Math.floor(t / 32)
  }
  return time + encodeBits(lastRandom)
}

function encodeBits(bytes: Uint8Array): string {
  let out = ''
  let buffer = 0
  let bits = 0
  for (const byte of bytes) {
    buffer = (buffer << 8) | byte
    bits += 8
    while (bits >= 5) {
      out += ALPHABET[(buffer >> (bits - 5)) & 31]
      bits -= 5
    }
  }
  return out
}
