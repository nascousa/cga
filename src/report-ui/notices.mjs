import {existsSync, mkdirSync, readdirSync, readFileSync, copyFileSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';

const root = resolve(import.meta.dirname);
const target = resolve(root, '../frontend/report-vendor/licenses');
mkdirSync(target, {recursive: true});
const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'));
const paths = Object.keys(lock.packages).filter(path => path && existsSync(join(root, path, 'package.json')))
  .map(path => join(root, path));
const packages = [];
for (const path of paths) {
  const pkg = JSON.parse(readFileSync(join(path, 'package.json'), 'utf8'));
  const name = pkg.name.replace(/[^a-zA-Z0-9._-]/g, '_') + '-' + pkg.version;
  const files = readdirSync(path, {withFileTypes: true}).filter(
    file => file.isFile() && /^(LICENSE|LICENCE|NOTICE|COPYING)(\.|-|$)/i.test(file.name),
  );
  const directory = join(target, name);
  mkdirSync(directory, {recursive: true});
  for (const file of files) copyFileSync(join(path, file.name), join(directory, file.name));
  packages.push({name: pkg.name, version: pkg.version, license: pkg.license, notices: files.map(file => name + '/' + file.name)});
}
writeFileSync(join(target, 'packages.json'), JSON.stringify(packages, null, 2) + '\n');
