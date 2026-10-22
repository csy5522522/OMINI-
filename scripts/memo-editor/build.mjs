import {build} from 'esbuild';
import {readFile, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
const here = fileURLToPath(new URL('.', import.meta.url));
await build({entryPoints:[here+'src.js'],outfile:here+'../../web/memo-editor.js',bundle:true,format:'iife',globalName:'OminiMemoEditor',target:['es2020'],minify:true,legalComments:'inline'});
const packages=['prosemirror-model','prosemirror-state','prosemirror-view','prosemirror-transform','prosemirror-history','prosemirror-keymap','prosemirror-commands','orderedmap','rope-sequence','w3c-keyname'];
const notices=await Promise.all(packages.map(async name=>`${name}\n${await readFile(here+'node_modules/'+name+'/LICENSE','utf8')}`));
await writeFile(here+'../../web/memo-editor.LICENSE.txt',notices.join('\n\n'));
