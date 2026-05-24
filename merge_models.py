#!/usr/bin/env python3
"""
ModelEngine Model Merger
=========================
将 ModelEngine 拆分成碎片的模型合并为完整单文件。

ModelEngine 将一个实体模型拆成：
  assets/modelengine/items/<model_name>/
    body.json       -> composite, 引用多个子模型
    bone1.json      -> 单个模型引用
    bone2.json      -> 单个模型引用
    ...

每个 part.json 内部引用：
  { "model": { "model": "ns:mdl/xxx", "type": "minecraft:model" } }
  或
  { "model": { "models": [...], "type": "minecraft:composite" } }

被引用的模型在 assets/<ns>/models/xxx.json 中包含实际的 elements。

本工具：
  1. 遍历 modelengine/items/ 下所有模型目录
  2. 解析每个 part.json，递归展开所有模型引用
  3. 将所有 elements 合并到单个 JSON 文件
  4. 保留 texture 映射关系
  5. 输出到 merged_models/ 目录
"""

import json
import os
import sys
import argparse
from collections import OrderedDict
from pathlib import Path


# ═══════════════════════════════════════════════════════════════════════════
# 1. 模型引用解析
# ═══════════════════════════════════════════════════════════════════════════

def resolve_model_ref(ref: str, output_dir: str) -> str:
    """
    将模型引用 'ns:mdl/xxx' 转为文件路径。
    例如 'jsmakehbp:mdl/0pj2plu7' -> 'output/assets/jsmakehbp/models/0pj2plu7.json'
    """
    if ':' not in ref:
        return None
    ns, path = ref.split(':', 1)
    # path is like 'mdl/xxx' or could be old format
    if path.startswith('mdl/'):
        model_name = path[4:]  # remove 'mdl/'
    else:
        model_name = path
    return os.path.join(output_dir, 'assets', ns, 'models', f'{model_name}.json')


def load_model_json(path: str) -> dict:
    """加载并解析一个模型 JSON 文件"""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def collect_all_elements(part_json: dict, output_dir: str, visited: set = None,
                         depth: int = 0) -> list:
    """
    递归收集一个 part.json 引用的所有 elements。
    
    处理的类型：
    - minecraft:model  -> 单个模型引用
    - minecraft:composite -> 多个模型引用
    
    返回: [(elements, textures, part_name), ...]
    """
    if visited is None:
        visited = set()
    if depth > 20:
        return []
    
    results = []
    model_info = part_json.get('model', {})
    model_type = model_info.get('type', '')
    
    if model_type == 'minecraft:model':
        ref = model_info.get('model', '')
        if ref and ref not in visited:
            visited.add(ref)
            model_path = resolve_model_ref(ref, output_dir)
            model_data = load_model_json(model_path)
            if model_data:
                elements = model_data.get('elements', [])
                textures = model_data.get('textures', {})
                results.append({
                    'elements': elements,
                    'textures': textures,
                    'source_ref': ref,
                    'source_path': model_path,
                })
    
    elif model_type == 'minecraft:composite':
        for sub_model in model_info.get('models', []):
            ref = sub_model.get('model', '')
            if ref and ref not in visited:
                visited.add(ref)
                model_path = resolve_model_ref(ref, output_dir)
                model_data = load_model_json(model_path)
                if model_data:
                    elements = model_data.get('elements', [])
                    textures = model_data.get('textures', {})
                    results.append({
                        'elements': elements,
                        'textures': textures,
                        'source_ref': ref,
                        'source_path': model_path,
                    })
    
    return results


# ═══════════════════════════════════════════════════════════════════════════
# 2. 合并模型
# ═══════════════════════════════════════════════════════════════════════════

def merge_model_dir(model_dir: str, model_name: str, output_dir: str) -> dict:
    """
    合并一个 ModelEngine 模型目录中的所有 part 为单一模型。
    
    返回合并后的模型 dict:
    {
        "credit": "ModelEngine Merger",
        "texture_size": [16, 16],
        "textures": { ... merged ... },
        "elements": [ ... all elements ... ],
        "_parts": [ ... part info ... ],
        "_stats": { ... }
    }
    """
    all_elements = []
    all_textures = {}
    parts_info = []
    visited = set()
    
    # 遍历目录下所有 JSON 文件
    part_files = sorted([
        f for f in os.listdir(model_dir) 
        if f.endswith('.json')
    ])
    
    for part_file in part_files:
        part_name = part_file[:-5]  # remove .json
        part_path = os.path.join(model_dir, part_file)
        
        try:
            with open(part_path, 'r', encoding='utf-8') as f:
                part_json = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        
        # 收集这个 part 引用的所有 elements
        collected = collect_all_elements(part_json, output_dir, visited)
        
        total_elements = 0
        refs = []
        for item in collected:
            elements = item['elements']
            textures = item['textures']
            ref = item['source_ref']
            
            # 为每个 element 添加来源标注
            for elem in elements:
                if '_part' not in elem:
                    elem['_part'] = part_name
                if '_source' not in elem:
                    elem['_source'] = ref
            
            all_elements.extend(elements)
            total_elements += len(elements)
            refs.append(ref)
            
            # 合并 textures (用编号避免冲突)
            for tex_key, tex_val in textures.items():
                # 使用 "partname.key" 避免不同 part 的 texture key 冲突
                merged_key = f"{part_name}.{tex_key}" if tex_key in all_textures else tex_key
                if merged_key not in all_textures:
                    all_textures[merged_key] = tex_val
        
        parts_info.append({
            'part_name': part_name,
            'elements_count': total_elements,
            'model_refs': refs,
        })
    
    # 构建合并后的模型
    merged = OrderedDict()
    merged['credit'] = 'ModelEngine Merger - deobf tool'
    merged['texture_size'] = [16, 16]
    merged['textures'] = all_textures
    merged['elements'] = all_elements
    merged['_model_name'] = model_name
    merged['_parts'] = parts_info
    merged['_stats'] = {
        'total_parts': len(part_files),
        'total_elements': len(all_elements),
        'total_textures': len(all_textures),
        'unique_model_refs': len(visited),
    }
    
    return merged


# ═══════════════════════════════════════════════════════════════════════════
# 3. 纹理解析 (尝试建立更可读的纹理名)
# ═══════════════════════════════════════════════════════════════════════════

def build_texture_index(output_dir: str) -> dict:
    """
    构建 texture ref -> 人类可读名的映射。
    遍历 assets/modelengine/textures/ 下的所有纹理文件。
    """
    tex_dir = os.path.join(output_dir, 'assets', 'modelengine', 'textures')
    index = {}
    if not os.path.isdir(tex_dir):
        return index
    
    for root, dirs, files in os.walk(tex_dir):
        for f in files:
            if f.endswith('.png'):
                rel = os.path.relpath(os.path.join(root, f), tex_dir)
                rel = rel.replace('\\', '/')
                # Remove .png extension
                if rel.endswith('.png'):
                    rel = rel[:-4]
                full_path = f'modelengine:{rel}'
                index[full_path] = f'modelengine:{rel}'
    
    return index


# ═══════════════════════════════════════════════════════════════════════════
# 4. 主逻辑
# ═══════════════════════════════════════════════════════════════════════════

def process_all_models(deobf_output: str, merge_output: str, verbose: bool = False):
    """处理所有 ModelEngine 模型"""
    
    me_items_dir = os.path.join(deobf_output, 'assets', 'modelengine', 'items')
    if not os.path.isdir(me_items_dir):
        print(f"Error: ModelEngine items directory not found: {me_items_dir}")
        print(f"Make sure you've run deobf.py first!")
        sys.exit(1)
    
    os.makedirs(merge_output, exist_ok=True)
    
    # 获取所有模型目录
    model_dirs = sorted([
        d for d in os.listdir(me_items_dir)
        if os.path.isdir(os.path.join(me_items_dir, d))
    ])
    
    print(f"Found {len(model_dirs)} ModelEngine models")
    print(f"Input:  {me_items_dir}")
    print(f"Output: {merge_output}")
    print()
    
    total_elements = 0
    total_parts = 0
    merged_count = 0
    empty_count = 0
    
    for model_name in model_dirs:
        model_dir = os.path.join(me_items_dir, model_name)
        part_count = len([f for f in os.listdir(model_dir) if f.endswith('.json')])
        
        if verbose:
            print(f"  Merging {model_name} ({part_count} parts)...", end=' ')
        
        merged = merge_model_dir(model_dir, model_name, deobf_output)
        
        elem_count = len(merged.get('elements', []))
        total_elements += elem_count
        total_parts += part_count
        
        if elem_count == 0:
            empty_count += 1
            if verbose:
                print("EMPTY (no elements found)")
            continue
        
        # 写入合并后的模型
        out_path = os.path.join(merge_output, f'{model_name}.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(merged, f, indent=2, ensure_ascii=False)
        
        merged_count += 1
        if verbose:
            print(f"OK ({elem_count} elements)")
    
    print()
    print(f"Results:")
    print(f"  Models merged:  {merged_count}")
    print(f"  Empty models:   {empty_count}")
    print(f"  Total parts:   {total_parts}")
    print(f"  Total elements: {total_elements}")
    print(f"  Output: {merge_output}")
    
    # 生成索引
    _write_index(merge_output, model_dirs, deobf_output)


def _write_index(merge_output: str, model_dirs: list, deobf_output: str):
    """生成模型索引"""
    index = {
        "description": "ModelEngine merged model index",
        "models": {}
    }
    
    for model_name in sorted(model_dirs):
        merged_path = os.path.join(merge_output, f'{model_name}.json')
        if os.path.isfile(merged_path):
            try:
                d = json.load(open(merged_path, 'r', encoding='utf-8'))
                stats = d.get('_stats', {})
                parts = d.get('_parts', [])
                index['models'][model_name] = {
                    'elements': stats.get('total_elements', 0),
                    'parts': stats.get('total_parts', 0),
                    'textures': stats.get('total_textures', 0),
                    'part_names': [p['part_name'] for p in parts],
                }
            except:
                pass
    
    with open(os.path.join(merge_output, '_index.json'), 'w', encoding='utf-8') as f:
        json.dump(index, f, indent=2, ensure_ascii=False)
    
    print(f"  Index: {os.path.join(merge_output, '_index.json')}")


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='ModelEngine Model Merger - Merge fragmented model parts into single files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python merge_models.py                           # Process output/ -> merged_models/
  python merge_models.py -i output -o merged       # Custom input/output
  python merge_models.py -v                        # Verbose mode

Prerequisites:
  Run deobf.py first to generate the output/ directory.
        """
    )
    parser.add_argument('-i', '--input', default='output',
                        help='Input directory (deobf.py output) (default: output)')
    parser.add_argument('-o', '--output', default='merged_models',
                        help='Output directory for merged models (default: merged_models)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose output')

    args = parser.parse_args()
    process_all_models(args.input, args.output, args.verbose)


if __name__ == '__main__':
    main()
