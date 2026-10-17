#!/usr/bin/env node
import { text } from 'node:stream/consumers'
import { main } from './main.ts'

process.exitCode = await main(process.argv.slice(2), {
  out: (output) => process.stdout.write(output),
  err: (output) => process.stderr.write(output),
  stdin: () => text(process.stdin),
})
