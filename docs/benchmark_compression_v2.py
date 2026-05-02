import os
import time
import subprocess
import shutil
import argparse
from pathlib import Path

def get_size(path):
    if os.path.isdir(path):
        return sum(f.stat().st_size for f in Path(path).rglob('*') if f.is_file())
    return os.path.getsize(path)

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

def run_cmd(cmd):
    start = time.time()
    try:
        subprocess.run(cmd, shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError as e:
        return None
    return time.time() - start

def benchmark(source_dir, results_file):
    temp_dir = "benchmark_temp"
    
    if not os.path.exists(source_dir):
        print(f"Error: {source_dir} not found.")
        return

    # Base commands
    # {l} is level, {out} is output file, {src} is source dir, {tmp} is temp extract dir
    configs = [
        # Zstd
        {"name": "zstd", "levels": [1, 3, 9, 15, 19], "ext": ".tar.zst", 
         "compress": "tar -cf - {src} | zstd -{l} -c > {out}", 
         "extract": "zstd -dc {out} | tar -xf - -C {tmp}"},
        
        # Gzip
        {"name": "gzip", "levels": [1, 6, 9], "ext": ".tar.gz",
         "compress": "tar -cf - {src} | gzip -{l} > {out}",
         "extract": "gunzip -c {out} | tar -xf - -C {tmp}"},
        
        # Parallel Gzip (pigz)
        {"name": "pigz", "levels": [1, 6, 9], "ext": ".tar.gz",
         "compress": "tar -cf - {src} | pigz -{l} > {out}",
         "extract": "unpigz -c {out} | tar -xf - -C {tmp}"},

        # Zip
        {"name": "zip", "levels": [1, 6, 9], "ext": ".zip",
         "compress": "zip -{l} -r {out} {src}",
         "extract": "unzip -q {out} -d {tmp}"},

        # XZ
        {"name": "xz", "levels": [0, 3, 6, 9], "ext": ".tar.xz",
         "compress": "tar -cf - {src} | xz -{l} > {out}",
         "extract": "xz -dc {out} | tar -xf - -C {tmp}"},

        # Bzip2
        {"name": "bzip2", "levels": [1, 9], "ext": ".tar.bz2",
         "compress": "tar -cf - {src} | bzip2 -{l} > {out}",
         "extract": "bzip2 -dc {out} | tar -xf - -C {tmp}"},

        # LZ4
        {"name": "lz4", "levels": [1, 9], "ext": ".tar.lz4",
         "compress": "tar -cf - {src} | lz4 -{l} > {out}",
         "extract": "lz4 -dc {out} | tar -xf - -C {tmp}"},

        # 7-Zip
        {"name": "7z", "levels": [1, 5, 9], "ext": ".7z",
         "compress": "7z a -mx={l} {out} {src}",
         "extract": "7z x {out} -o{tmp}"},
        
        # Bgzip
        {"name": "bgzip", "levels": [1, 6, 9], "ext": ".tar.bgz",
         "compress": "tar -cf - {src} | bgzip -l {l} -c > {out}",
         "extract": "bgzip -dc {out} | tar -xf - -C {tmp}"}
    ]

    results = []
    raw_size = get_size(source_dir)
    
    print(f"Benchmarking source: {source_dir}")
    print(f"Raw size: {format_bytes(raw_size)}")
    print("-" * 80)
    print(f"{'Method':<15} {'Level':<5} {'Size':<12} {'Ratio':<8} {'Comp (s)':<10} {'Ext (s)':<10}")
    print("-" * 80)

    for config in configs:
        # Check if base tool exists
        tool = config["name"]
        if tool == "pigz": tool_cmd = "pigz"
        elif tool == "bgzip": tool_cmd = "bgzip"
        elif tool == "7z": tool_cmd = "7z"
        elif tool == "lz4": tool_cmd = "lz4"
        else: tool_cmd = config["name"].split()[0]

        if shutil.which(tool_cmd) is None:
            continue

        for level in config["levels"]:
            out_file = f"test_result_{config['name']}_l{level}{config['ext']}"
            
            if os.path.exists(out_file): os.remove(out_file)
            if os.path.exists(temp_dir): shutil.rmtree(temp_dir)
            os.makedirs(temp_dir)

            comp_time = run_cmd(config["compress"].format(out=out_file, src=source_dir, l=level))
            if comp_time is None:
                print(f"{config['name']:<15} {level:<5} FAILED")
                continue
            
            size = get_size(out_file)
            ratio = raw_size / size if size > 0 else 0

            extract_time = run_cmd(config["extract"].format(out=out_file, tmp=temp_dir))
            if extract_time is None:
                print(f"{config['name']:<15} {level:<5} EXTRACT FAILED")
                continue

            print(f"{config['name']:<15} {level:<5} {format_bytes(size):<12} {ratio:>6.2f}x {comp_time:>10.2f} {extract_time:>10.2f}")

            results.append({
                "Method": config["name"],
                "Level": level,
                "Size": format_bytes(size),
                "Size_B": size,
                "Ratio": f"{ratio:.2f}x",
                "Comp_s": f"{comp_time:.2f}",
                "Ext_s": f"{extract_time:.2f}"
            })

            if os.path.exists(out_file): os.remove(out_file)
            if os.path.exists(temp_dir): shutil.rmtree(temp_dir)

    # Save to Markdown
    with open(results_file, "w") as f:
        f.write("# Advanced Compression Benchmark\n\n")
        f.write(f"Source: `{source_dir}` (Raw size: {format_bytes(raw_size)})\n\n")
        f.write("| Method | Level | Size | Ratio | Comp Time (s) | Ext Time (s) |\n")
        f.write("|:---|:---|:---|:---|:---|:---|\n")
        for r in sorted(results, key=lambda x: x['Size_B']):
            f.write(f"| {r['Method']} | {r['Level']} | {r['Size']} | {r['Ratio']} | {r['Comp_s']} | {r['Ext_s']} |\n")
    
    print(f"\nResults saved to {results_file} (sorted by size)")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Advanced Benchmark for compression algorithms.")
    parser.add_argument("source", nargs="?", default="reference/microarray/raw_file_templates", help="Source directory to compress")
    parser.add_argument("--output", default="advanced_benchmark_results.md", help="Results file")
    args = parser.parse_args()
    
    benchmark(args.source, args.output)
