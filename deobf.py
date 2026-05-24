#!/usr/bin/env python3
"""
CraftEngine Pro ResourcePack Deobfuscator
==========================================
针对 nodemcsource.zip 的反混淆 / 路径重构工具

功能:
  1. 直接遍历 ZIP Central Directory，无需真正解压
  2. 路径压缩: 将 a/b/c/d/e/f/ 等单字符/随机目录合并
  3. Unicode-escape JSON 解码 (\\uXXXX -> 可读文本)
  4. 输出重构后的资源树 + 映射表
  5. 自动建立 texture 引用关系
  6. 支持 ZIP64 / 超长路径 / UTF-8 ZIP
"""

import zipfile
import json
import os
import sys
import re
import hashlib
import argparse
from collections import defaultdict
from pathlib import PurePosixPath

# ─── 语义目录（保留不合并） ─────────────────────────────────────────────────
SEMANTIC_DIRS = {
    "assets", "models", "textures", "item", "block", "font", "shaders",
    "sounds", "blockstates", "atlases", "lang", "particles", "equipment",
    "misc", "obf", "core", "include", "entity", "humanoid",
    "humanoid_leggings", "trims", "armor", "custom",
}

# Minecraft 标准命名空间（保留不压缩）
MC_NAMESPACES = {"minecraft", "modelengine"}

# CraftEngine 已知的混淆命名空间（需要反混淆的）
KNOWN_OBF_NS_PREFIXES = (
    # 10-char random looking ones
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Unicode Escape 解码
# ═══════════════════════════════════════════════════════════════════════════

def decode_unicode_json(raw_bytes: bytes) -> object:
    """Parse JSON and recursively decode all \\uXXXX sequences in strings."""
    obj = json.loads(raw_bytes)
    return _decode_recursive(obj)


def _decode_recursive(obj):
    if isinstance(obj, str):
        # The JSON is stored with literal \\uXXXX in the file.
        # json.loads() already decodes \uXXXX, but CraftEngine stores
        # double-escaped \\\\uXXXX so after json.loads we still have
        # literal \uXXXX strings. We need to decode them again.
        # However json.loads already decoded them once...
        # Let's check: the raw bytes have \\\\u which json.loads turns into
        # the literal characters \uXXXX. So we need to re-parse those.
        return _try_decode_unicode_escapes(obj)
    elif isinstance(obj, dict):
        return {_decode_recursive(k): _decode_recursive(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_decode_recursive(item) for item in obj]
    return obj


def _try_decode_unicode_escapes(s: str) -> str:
    """Decode any remaining \\uXXXX sequences in a string."""
    # Pattern: literal backslash + u + 4 hex digits
    pattern = re.compile(r'\\u([0-9a-fA-F]{4})')

    def replacer(m):
        return chr(int(m.group(1), 16))

    if '\\u' not in s:
        return s

    result = pattern.sub(replacer, s)

    # Repeat in case of double-encoding
    max_rounds = 5
    for _ in range(max_rounds):
        if '\\u' not in result:
            break
        new_result = pattern.sub(replacer, result)
        if new_result == result:
            break
        result = new_result

    return result


# ═══════════════════════════════════════════════════════════════════════════
# 2. 路径压缩 (Path Compaction)
# ═══════════════════════════════════════════════════════════════════════════

def is_single_char_dir(name: str) -> bool:
    """Check if a directory name is a single character (obfuscation pattern)."""
    return len(name) == 1 and name.isalnum()


def looks_random(name: str) -> bool:
    """Heuristic: does this directory name look like random obfuscation?"""
    # Random names are typically lowercase alphanumeric, length <= 10,
    # with no recognizable English words
    if len(name) <= 1:
        return True
    if name in SEMANTIC_DIRS:
        return False
    # All lowercase + digits, no vowels pattern (consonant-heavy)
    if re.match(r'^[a-z0-9]+$', name):
        # Check consonant ratio
        vowels = sum(1 for c in name if c in 'aeiou')
        if len(name) > 3 and vowels / len(name) < 0.2:
            return True
        # Check if it's a known MC word
        common = {'block', 'item', 'door', 'trap', 'fence', 'stairs',
                  'slab', 'wall', 'sign', 'chest', 'log', 'planks',
                  'stone', 'dirt', 'sand', 'glass', 'wool', 'iron',
                  'gold', 'diamond', 'oak', 'birch', 'spruce', 'dark'}
        if name in common:
            return False
        # Short alphanumeric that doesn't match common patterns
        if len(name) <= 10:
            return True
    return False


def compact_path(original_path: str) -> str:
    """
    Compress an obfuscated path.

    Rules:
    - Preserve: assets/, models/, textures/, item/, block/, etc.
    - Merge consecutive single-char dirs: a/b/c/d/ -> abcd/
    - Merge consecutive random dirs: x1y2z/abc/ -> x1y2zabc/
    - '...' is an obfuscation marker meaning "more random dirs here" -> merge into neighbors
    - Keep meaningful dirs intact
    - Flatten ..json / ..png / ..ogg suffixes to normal extensions
    - If filename has no basename (e.g. ".json"), use last merged dir segment as name
    """
    parts = original_path.split('/')

    # Fix the obfuscated file extension: ..json -> .json, ..png -> .png
    filename = parts[-1] if parts else ''
    fname_fixed = _fix_obfuscated_ext(filename)
    if parts:
        parts[-1] = fname_fixed

    # Separate into dir-parts and filename
    dir_parts = parts[:-1]
    fname = parts[-1] if parts else ''

    if not dir_parts:
        return original_path

    # Remove '...' segments (they're obfuscation markers, not real dirs)
    cleaned = []
    for part in dir_parts:
        if part == '...':
            continue  # skip obfuscation marker
        cleaned.append(part)

    # Now compress consecutive random/single-char segments
    result_parts = []
    merge_buffer = []

    for part in cleaned:
        if part in SEMANTIC_DIRS or part in MC_NAMESPACES or not looks_random(part):
            # Flush buffer
            if merge_buffer:
                result_parts.append(_merge_buffer(merge_buffer))
                merge_buffer = []
            result_parts.append(part)
        else:
            merge_buffer.append(part)

    if merge_buffer:
        merged = _merge_buffer(merge_buffer)
        # If filename is just an extension (e.g. ".json"), use last merged
        # segment as the basename
        # If filename has no real basename (e.g. ".json" or ".png.mcmeta"),
        # use last merged segment as the basename
        fname_base = fname.split('.')[0]
        if not fname_base:
            # fname is like ".json" - need a basename
            # Use the last segment of merged as the basename
            segments = merged.split('/')
            basename = segments[-1]
            if len(segments) > 1:
                result_parts.append('/'.join(segments[:-1]))
            # Generate a descriptive name
            ext = fname  # e.g. ".json"
            fname = f"{basename}{ext}"
        else:
            result_parts.append(merged)

    return '/'.join(result_parts + [fname])


def _fix_obfuscated_ext(filename: str) -> str:
    """Fix obfuscated file extensions like ..json -> .json
    
    Returns (basename, extension). For obfuscated files like '..json',
    basename will be empty and the caller should generate one from merged dirs.
    """
    obf_patterns = [
        ('..png.mcmeta', '.png.mcmeta'),
        ('..json', '.json'),
        ('..png', '.png'),
        ('..ogg', '.ogg'),
        ('..mcmeta', '.mcmeta'),
        ('..fsh', '.fsh'),
        ('..vsh', '.vsh'),
        ('..glsl', '.glsl'),
    ]
    for old, new in obf_patterns:
        if filename.endswith(old):
            return filename[:-len(old)] + new
    return filename


def _merge_buffer(buf: list) -> str:
    """Merge a list of single-char / random directory names into a compact form."""
    if not buf:
        return ''
    merged = ''.join(buf)
    # If merged is very long, split into chunks of ~8 chars
    if len(merged) > 12:
        chunks = []
        for i in range(0, len(merged), 8):
            chunks.append(merged[i:i+8])
        return '/'.join(chunks)
    return merged


# ═══════════════════════════════════════════════════════════════════════════
# 3. 引用关系分析
# ═══════════════════════════════════════════════════════════════════════════

def _rewrite_tex_refs(obj, tex_ref_map: dict):
    """Recursively rewrite obfuscated references in a JSON object."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k == 'textures' and isinstance(v, dict):
                new_tex = {}
                for tk, tv in v.items():
                    if isinstance(tv, str) and tv in tex_ref_map:
                        new_tex[tk] = tex_ref_map[tv]
                    elif isinstance(tv, str) and ':' in tv and 'obf/' in tv:
                        new_tex[tk] = _compact_tex_ref(tv)
                    else:
                        new_tex[tk] = _rewrite_tex_refs(tv, tex_ref_map)
                result[k] = new_tex
            else:
                result[k] = _rewrite_tex_refs(v, tex_ref_map)
        return result
    elif isinstance(obj, list):
        return [_rewrite_tex_refs(item, tex_ref_map) for item in obj]
    elif isinstance(obj, str):
        # Rewrite model references: "ns:x/y/.../z/."
        if obj in tex_ref_map:
            return tex_ref_map[obj]
        if ':' in obj and '/.../' in obj:
            # Could be model or texture ref with obfuscation
            if 'obf/' in obj:
                return _compact_tex_ref(obj)
            else:
                return _compact_model_ref(obj)
        return obj
    return obj


def _compact_tex_ref(ref: str) -> str:
    """Compact an obfuscated texture reference like '7w5x0vpi5:obf/r/2/9/t/t/.../h/0/8/.'"""
    if ':' not in ref or 'obf/' not in ref:
        return ref
    ns, path = ref.split(':', 1)
    # path is like 'obf/r/2/9/t/t/.../h/0/8/.'
    # Remove 'obf/' prefix
    if path.startswith('obf/'):
        path = path[4:]
    # Remove trailing '.'
    path = path.rstrip('.')
    # Remove '...' marker
    path = path.replace('...', '')
    # Split and merge single-char dirs
    parts = path.split('/')
    merged = ''.join(parts)
    # Split into chunks if too long
    if len(merged) > 12:
        chunks = [merged[i:i+8] for i in range(0, len(merged), 8)]
        path = '/'.join(chunks)
    else:
        path = merged
    return f"{ns}:tex/{path}"


def _compact_model_ref(ref: str) -> str:
    """Compact an obfuscated model reference like 'gwjritg0z:r/1/m/.../i/8/2/l/1/.'"""
    if ':' not in ref:
        return ref
    ns, path = ref.split(':', 1)
    # path is like 'r/1/m/.../i/8/2/l/1/.'
    # Remove trailing '.'
    path = path.rstrip('.')
    # Remove '...' marker
    path = path.replace('...', '')
    # Split and merge single-char dirs
    parts = path.split('/')
    merged = ''.join(parts)
    if len(merged) > 12:
        chunks = [merged[i:i+8] for i in range(0, len(merged), 8)]
        path = '/'.join(chunks)
    else:
        path = merged
    return f"{ns}:mdl/{path}"


def resolve_mc_texture_ref(ref: str) -> str:
    """
    Resolve a Minecraft texture reference.
    'namespace:path' -> 'assets/namespace/textures/path.png'
    'block/oak_log'  -> 'assets/minecraft/textures/block/oak_log.png'
    """
    if ':' in ref:
        ns, path = ref.split(':', 1)
    else:
        ns = 'minecraft'
        path = ref

    # Remove obf/ prefix if present
    # obf/ is a CraftEngine indicator that the path is obfuscated
    return f'assets/{ns}/textures/{path}'


def resolve_mc_model_ref(ref: str) -> str:
    """
    Resolve a Minecraft model reference.
    'namespace:path' -> 'assets/namespace/models/path.json'
    """
    if ':' in ref:
        ns, path = ref.split(':', 1)
    else:
        ns = 'minecraft'
        path = ref
    return f'assets/{ns}/models/{path}.json'


# ═══════════════════════════════════════════════════════════════════════════
# 4. 核心: ZIP 遍历 + 反混淆
# ═══════════════════════════════════════════════════════════════════════════

def process_zip(zip_path: str, output_dir: str, verbose: bool = False):
    """Main processing function."""

    print(f"[1/5] Opening ZIP: {zip_path}")
    z = zipfile.ZipFile(zip_path, 'r')
    all_names = z.namelist()
    print(f"      Total entries: {len(all_names)}")

    # Classify entries
    obf_entries = [n for n in all_names if '...' in n]
    normal_entries = [n for n in all_names if '...' not in n]
    print(f"      Obfuscated: {len(obf_entries)}")
    print(f"      Normal: {len(normal_entries)}")

    # Build mapping: original_path -> compact_path
    path_map = {}
    # Also track: compact_path -> [original_paths] for collision detection
    compact_to_original = defaultdict(list)

    print(f"\n[2/5] Building path mappings...")

    for entry in all_names:
        # Strip trailing / from misnamed file entries (like sounds.json/)
        entry_name = entry.rstrip('/')
        
        # Skip pure directory entries (ending with /)
        if entry.endswith('/'):
            # But some ZIPs store actual files with trailing / (like sounds.json/)
            # Check size: if >0, it's a misnamed file
            info = z.getinfo(entry)
            if info.file_size == 0 and info.compress_size == 0:
                continue
        # Skip zero-size entries with no extension (they're directory markers)
        info = z.getinfo(entry)
        fname = entry_name.split('/')[-1]
        has_ext = '.' in fname and not fname.startswith('.')
        if info.file_size == 0 and not has_ext:
            continue
        compact = compact_path(entry_name)
        path_map[entry] = compact
        compact_to_original[compact].append(entry)

    # Detect and fix collisions
    collisions = {k: v for k, v in compact_to_original.items() if len(v) > 1}
    if collisions:
        print(f"      [!] {len(collisions)} path collisions detected, fixing...")
        for compact, originals in collisions.items():
            for i, orig in enumerate(originals):
                if i == 0:
                    path_map[orig] = compact
                else:
                    # Add hash suffix for disambiguation
                    h = hashlib.md5(orig.encode()).hexdigest()[:6]
                    base, ext = os.path.splitext(compact)
                    path_map[orig] = f"{base}_{h}{ext}"

    print(f"      Mapped {len(path_map)} file entries")

    # Build a lookup: obfuscated texture path -> compact texture path
    # Texture references in JSON look like: "7w5x0vpi5:obf/r/2/9/t/t/.../h/0/8/."
    # Which maps to: assets/7w5x0vpi5/textures/obf/r/2/9/t/t/.../h/0/8/..png
    tex_ref_map = {}  # "namespace:path" -> compact relative path
    for orig, compact in path_map.items():
        entry_name = orig.rstrip('/')
        # Only process texture files
        if '/textures/' not in entry_name:
            continue
        # Build the MC resource reference: namespace:path
        parts = entry_name.split('/')
        if len(parts) >= 4 and parts[0] == 'assets':
            ns = parts[1]  # e.g. '7w5x0vpi5'
            # path is everything after 'textures/' with extension stripped
            tex_idx = parts.index('textures') if 'textures' in parts else -1
            if tex_idx >= 0:
                tex_path = '/'.join(parts[tex_idx+1:])
                # Remove the extension (.png or ..png)
                if tex_path.endswith('..png'):
                    tex_path = tex_path[:-5]  # remove ..png
                elif tex_path.endswith('.png'):
                    tex_path = tex_path[:-4]  # remove .png
                # Also handle trailing dot in obf refs
                # The reference "7w5x0vpi5:obf/r/.../h/0/8/." has trailing "."
                # which maps to path "obf/r/.../h/0/8/."
                ref_key = f"{ns}:{tex_path}"
                # Compact path relative to assets/ns/textures/
                compact_parts = compact.split('/')
                if 'textures' in compact_parts:
                    ti = compact_parts.index('textures')
                    compact_rel = '/'.join(compact_parts[ti+1:])
                    # Remove extension
                    if compact_rel.endswith('.png'):
                        compact_rel = compact_rel[:-4]
                    tex_ref_map[ref_key] = compact_rel

    print(f"      Built texture reference map: {len(tex_ref_map)} entries")

    # Build reference graph from JSON files
    print(f"\n[3/5] Analyzing JSON references...")
    ref_graph = defaultdict(dict)  # compact_path -> {type: [refs]}

    for orig, compact in path_map.items():
        if not orig.endswith('.json') and not orig.endswith('..json'):
            continue
        if orig.endswith('..json') or orig.endswith('.json'):
            try:
                raw = z.read(orig)
                obj = decode_unicode_json(raw)

                # Extract texture references
                textures = obj.get('textures', {}) if isinstance(obj, dict) else {}
                if not isinstance(textures, dict):
                    textures = {}
                tex_refs = []
                for key, val in textures.items():
                    if isinstance(val, str) and (':' in val or
                        val.startswith('block/') or val.startswith('item/')):
                        tex_refs.append((key, val))
                if tex_refs:
                    ref_graph[compact]['textures'] = tex_refs

                # Extract parent references
                parent = obj.get('parent', None) if isinstance(obj, dict) else None
                if parent:
                    ref_graph[compact]['parent'] = parent

                # Extract elements count
                elements = obj.get('elements', []) if isinstance(obj, dict) else []
                if elements:
                    ref_graph[compact]['elements_count'] = len(elements)

            except (json.JSONDecodeError, KeyError, UnicodeDecodeError) as e:
                if verbose:
                    print(f"      [!] Failed to parse {orig}: {e}")

    print(f"      Found references in {len(ref_graph)} JSON files")

    # ─── Write output ──────────────────────────────────────────────────
    print(f"\n[4/5] Writing output to: {output_dir}")

    os.makedirs(output_dir, exist_ok=True)

    # Write files - collect first, then write to handle file/dir conflicts
    errors = 0
    write_plan = []
    for orig, compact in path_map.items():
        out_path = os.path.join(output_dir, compact)
        out_path = os.path.normpath(out_path)

        # Security: prevent path traversal
        if not out_path.startswith(os.path.normpath(output_dir)):
            continue

        try:
            raw = z.read(orig)

            # Process JSON files
            if orig.endswith('.json') or orig.endswith('..json') or orig.endswith('.mcmeta'):
                try:
                    obj = decode_unicode_json(raw)
                    # Rewrite texture references to use compact paths
                    obj = _rewrite_tex_refs(obj, tex_ref_map)
                    raw = json.dumps(obj, indent=2, ensure_ascii=False).encode('utf-8')
                except (json.JSONDecodeError, UnicodeDecodeError):
                    pass

            write_plan.append((out_path, raw))
        except Exception as e:
            errors += 1
            if verbose or errors <= 10:
                print(f"      [!] Error reading {orig}: {e}")

    # Sort by path length to ensure parent dirs are created first
    write_plan.sort(key=lambda x: len(x[0]))

    written = 0
    errors = 0
    dirs_created = set()

    for out_path, raw in write_plan:
        try:
            parent = os.path.dirname(out_path)
            # Handle conflict: if a file exists where we need a directory, remove it
            if parent not in dirs_created:
                if os.path.exists(parent) and not os.path.isdir(parent):
                    os.remove(parent)
                os.makedirs(parent, exist_ok=True)
                dirs_created.add(parent)
                # Also add all ancestor dirs
                p = parent
                while p and p not in dirs_created:
                    if os.path.exists(p) and not os.path.isdir(p):
                        os.remove(p)
                    dirs_created.add(p)
                    p = os.path.dirname(p)

            # Handle conflict: if a directory exists where we need a file
            if os.path.isdir(out_path):
                import shutil
                shutil.rmtree(out_path)

            with open(out_path, 'wb') as f:
                f.write(raw)
            written += 1

        except Exception as e:
            errors += 1
            if verbose or errors <= 10:
                print(f"      [!] Error writing {out_path}: {e}")

    print(f"      Written: {written} files")
    if errors:
        print(f"      Errors: {errors}")

    # ─── Write mapping table ────────────────────────────────────────────
    print(f"\n[5/5] Writing mapping tables...")
    _write_mapping(output_dir, path_map, ref_graph)

    z.close()
    print(f"\n[OK] Done! Output in: {output_dir}")


def _write_mapping(output_dir: str, path_map: dict, ref_graph: dict):
    """Write mapping files."""

    # 1. Path mapping JSON
    mapping = {
        "description": "CraftEngine Pro ResourcePack path mapping (original -> compact)",
        "total_files": len(path_map),
        "mappings": {}
    }
    for orig in sorted(path_map.keys()):
        compact = path_map[orig]
        if orig != compact:
            mapping["mappings"][orig] = compact

    with open(os.path.join(output_dir, '_path_mapping.json'), 'w', encoding='utf-8') as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)

    # 2. Reverse mapping (compact -> original)
    reverse = {}
    for orig, compact in path_map.items():
        if orig != compact:
            reverse[compact] = orig

    with open(os.path.join(output_dir, '_reverse_mapping.json'), 'w', encoding='utf-8') as f:
        json.dump(reverse, f, indent=2, ensure_ascii=False)

    # 3. Reference graph
    with open(os.path.join(output_dir, '_references.json'), 'w', encoding='utf-8') as f:
        json.dump(ref_graph, f, indent=2, ensure_ascii=False)

    # 4. Human-readable report
    with open(os.path.join(output_dir, '_report.txt'), 'w', encoding='utf-8') as f:
        f.write("CraftEngine Pro ResourcePack Deobfuscation Report\n")
        f.write("=" * 60 + "\n\n")

        # Stats
        obf_count = sum(1 for o, c in path_map.items() if o != c)
        f.write(f"Total files: {len(path_map)}\n")
        f.write(f"Obfuscated (paths changed): {obf_count}\n")
        f.write(f"Unchanged: {len(path_map) - obf_count}\n\n")

        # Namespace summary
        namespaces = defaultdict(lambda: {"models": 0, "textures": 0, "sounds": 0, "other": 0})
        for orig, compact in path_map.items():
            parts = orig.split('/')
            if len(parts) >= 3 and parts[0] == 'assets':
                ns = parts[1]
                cat = parts[2]
                if cat == 'models':
                    namespaces[ns]["models"] += 1
                elif cat == 'textures':
                    namespaces[ns]["textures"] += 1
                elif cat == 'sounds':
                    namespaces[ns]["sounds"] += 1
                else:
                    namespaces[ns]["other"] += 1

        f.write("Namespace Summary:\n")
        f.write("-" * 60 + "\n")
        for ns in sorted(namespaces.keys()):
            info = namespaces[ns]
            total = sum(info.values())
            f.write(f"  {ns}: {total} files")
            details = [f"{k}={v}" for k, v in info.items() if v > 0]
            f.write(f" ({', '.join(details)})\n")

        f.write(f"\n\nPath Mappings (obfuscated only):\n")
        f.write("-" * 60 + "\n")
        for orig in sorted(path_map.keys()):
            compact = path_map[orig]
            if orig != compact:
                f.write(f"  {orig}\n    -> {compact}\n")

        # Texture references
        f.write(f"\n\nTexture Reference Graph:\n")
        f.write("-" * 60 + "\n")
        for compact in sorted(ref_graph.keys()):
            info = ref_graph[compact]
            f.write(f"  {compact}\n")
            if 'textures' in info:
                for key, ref in info['textures']:
                    f.write(f"    texture[{key!r}]: {ref}\n")
            if 'parent' in info:
                f.write(f"    parent: {info['parent']}\n")
            if 'elements_count' in info:
                f.write(f"    elements: {info['elements_count']}\n")

    print(f"  _path_mapping.json    - Original -> Compact path mapping")
    print(f"  _reverse_mapping.json - Compact -> Original path mapping")
    print(f"  _references.json      - Texture/parent reference graph")
    print(f"  _report.txt           - Human-readable report")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='CraftEngine Pro ResourcePack Deobfuscator',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python deobf.py                          # Process nodemcsource.zip -> output/
  python deobf.py -i mypack.zip -o result  # Custom input/output
  python deobf.py -v                       # Verbose mode
        """
    )
    parser.add_argument('-i', '--input', default='nodemcsource.zip',
                        help='Input ZIP file (default: nodemcsource.zip)')
    parser.add_argument('-o', '--output', default='output',
                        help='Output directory (default: output)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose output')

    args = parser.parse_args()

    # Auto-detect ZIP in current directory
    zip_path = args.input
    if not os.path.isfile(zip_path):
        # Try to find any .zip in current directory
        import glob
        zips = glob.glob('*.zip')
        if len(zips) == 1:
            zip_path = zips[0]
            print(f"Auto-detected: {zip_path}")
        elif len(zips) > 1:
            print(f"Multiple ZIP files found: {zips}")
            print(f"Please specify with -i <file>")
            sys.exit(1)
        else:
            print(f"Error: No ZIP file found")
            sys.exit(1)

    process_zip(zip_path, args.output, args.verbose)


if __name__ == '__main__':
    main()
