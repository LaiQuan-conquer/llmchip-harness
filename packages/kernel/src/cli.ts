#!/usr/bin/env node
import { main } from './main.ts'

process.exitCode = main(process.argv.slice(2), {
  out: (text) => process.stdout.write(text),
  err: (text) => process.stderr.write(text),
})
