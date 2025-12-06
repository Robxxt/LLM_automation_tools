#!/usr/bin/env python3
import os
import re
import json

# --- Configuration ---
PROJECT_ROOT = "."
LIB_DIR = os.path.join(PROJECT_ROOT, "lib")
PUBSPEC_FILE = os.path.join(PROJECT_ROOT, "pubspec.yaml")
OUTPUT_FILE = "PROJECT_METADATA.json"

# Exclusions
TREE_IGNORE_DIRS = {'test', 'android', 'ios', 'windows', 'linux', 'macos', '.dart_tool', 'build', '.git', '.idea'}
TREE_IGNORE_EXTS = {'.g.dart', '.freezed.dart', '.png', '.jpg', '.jpeg', '.svg', '.ico', '.json'}

# --- Regex Patterns ---

# 1. General
IMPORT_PATTERN = re.compile(r"import\s+['\"]([^'\"]+)['\"];")

# 2. Models & Enums
# Captures @HiveField(0) final type name;
HIVE_FIELD_PATTERN = re.compile(r'@HiveField\((\d+)\)\s*(?:final\s+)?([\w<>,? ]+)\s+(\w+);')
# Captures standard fields
STD_FIELD_PATTERN = re.compile(r'(?:final\s+)?([\w<>,? ]+)\s+(\w+);')
# Captures enum Name { ... }
ENUM_PATTERN = re.compile(r'enum\s+(\w+)\s*\{([\s\S]*?)\}', re.MULTILINE)

# 3. Providers
# Captures @riverpod class Name extends _$Parent
RIVERPOD_CLASS_PATTERN = re.compile(r'@(?:r|R)iverpod\s*(?:\(([\s\S]*?)\))?\s*class\s+(\w+)\s+extends\s+_\$(\w+)', re.MULTILINE)
# Captures method signatures
METHOD_PATTERN = re.compile(r'^\s*(?:Future<[\w<>?, ]+>|void|[\w<>?]+)\s+([a-zA-Z0-9_]+)\s*\(([^)]*)\)', re.MULTILINE)

# 4. Hive Registry
HIVE_TYPE_PATTERN = re.compile(r'@HiveType\s*\(\s*typeId\s*:\s*(\d+)\s*\)[\s\S]*?class\s+(\w+)', re.MULTILINE)

# 5. Router
GOROUTE_PATTERN = re.compile(r"GoRoute\s*\([\s\S]*?path:\s*['\"]([^'\"]+)['\"][\s\S]*?=>\s*(?:const\s+)?(\w+)\(", re.MULTILINE)


class ProjectScanner:
    def __init__(self):
        # The main data structure to be dumped to JSON
        self.data = {
            "project_name": os.path.basename(os.path.abspath(PROJECT_ROOT)),
            "manifest": {},
            "files": [],
            "providers": {},
            "models": {}, # Includes Enums
            "routing": {},
            "hive_registry": {}
        }
    
    def read_file(self, path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception:
            return ""

    def get_imports(self, content):
        """Extracts list of imported file paths."""
        return [m for m in IMPORT_PATTERN.findall(content) if 'dart:' not in m and 'flutter/' not in m]

    # --- Feature: Manifest ---
    def scan_manifest(self):
        if not os.path.exists(PUBSPEC_FILE):
            return

        content = self.read_file(PUBSPEC_FILE)
        dependencies = {}
        in_deps = False
        
        for line in content.splitlines():
            l = line.strip()
            if l.startswith('dependencies:'):
                in_deps = True
                continue
            if in_deps and (l.startswith('dev_dependencies:') or l == ''):
                in_deps = False
                continue
            
            if in_deps and ':' in l:
                k, v = l.split(':', 1)
                dependencies[k.strip()] = v.strip()
        
        self.data['manifest'] = dependencies

    # --- Feature: Hive Registry ---
    def scan_hive_registry_global(self):
        registry = {}
        for root, _, files in os.walk(LIB_DIR):
            for file in files:
                if file.endswith('.dart') and not file.endswith('.g.dart'):
                    content = self.read_file(os.path.join(root, file))
                    for match in HIVE_TYPE_PATTERN.finditer(content):
                        type_id = int(match.group(1))
                        class_name = match.group(2)
                        rel_path = os.path.relpath(os.path.join(root, file), PROJECT_ROOT)
                        
                        registry[str(type_id)] = {
                            "className": class_name,
                            "file": rel_path
                        }
        self.data['hive_registry'] = registry

    # --- Feature: Data Models (Classes + Enums) ---
    def extract_models(self):
        # We assume models are primarily in lib/data/models, but we can scan lib if needed.
        # For efficiency, let's stick to scanning recursively from LIB_DIR but filtering for "content"
        
        for root, _, files in os.walk(LIB_DIR):
            for file_name in files:
                if file_name.endswith('.dart') and not file_name.endswith('.g.dart'):
                    file_path = os.path.join(root, file_name)
                    content = self.read_file(file_path)
                    
                    # 1. Parse Enums
                    for enum_match in ENUM_PATTERN.finditer(content):
                        e_name = enum_match.group(1)
                        e_body = enum_match.group(2)
                        # Clean comments and parse values
                        e_body = re.sub(r'//.*', '', e_body)
                        values = re.findall(r'\b[a-zA-Z0-9_]+\b', e_body)
                        
                        self.data['models'][e_name] = {
                            "type": "Enum",
                            "values": values,
                            "file": os.path.relpath(file_path, PROJECT_ROOT)
                        }

                    # 2. Parse Classes (only if they seem to be models)
                    # We check if they have fields or extend something relevant
                    class_matches = re.finditer(r'class\s+(\w+)\s+(?:extends|implements|with)\s+([^\{]+)', content)
                    for class_match in class_matches:
                        class_name = class_match.group(1)
                        
                        # Find constructor to extract fields
                        ctor_match = re.search(rf'{class_name}\s*\(\s*\{{([\s\S]*?)\}}\s*\)', content)
                        
                        if ctor_match:
                            fields = {}
                            params = re.findall(r'this\.(\w+)', ctor_match.group(1))
                            
                            for p in params:
                                # Try Hive Field first
                                h_match = re.search(rf'@HiveField\((\d+)\)\s*(?:final\s+)?([\w<>,? ]+)\s+{p}\s*;', content)
                                if h_match:
                                    fields[p] = {
                                        "type": h_match.group(2).strip(),
                                        "hiveId": int(h_match.group(1))
                                    }
                                    continue
                                
                                # Try Standard Field
                                s_match = re.search(rf'(?:final\s+)?([\w<>,? ]+)\s+{p}\s*;', content)
                                if s_match:
                                    fields[p] = s_match.group(1).strip()
                                else:
                                    fields[p] = "dynamic"
                            
                            if fields:
                                self.data['models'][class_name] = {
                                    "type": "Class",
                                    "fields": fields,
                                    "file": os.path.relpath(file_path, PROJECT_ROOT)
                                }

    # --- Feature: Providers ---
    def scan_providers(self):
        for root, _, files in os.walk(LIB_DIR):
            for file_name in files:
                # Filter for likely provider files to save regex time
                if "provider" in file_name or "controller" in file_name:
                    path = os.path.join(root, file_name)
                    content = self.read_file(path)
                    rel_path = os.path.relpath(path, PROJECT_ROOT)
                    
                    provider_entries = []
                    
                    for match in RIVERPOD_CLASS_PATTERN.finditer(content):
                        args = match.group(1) or ""
                        class_name = match.group(2)
                        
                        is_keep_alive = "keepAlive: true" in args
                        
                        # Extract State Type (Return of build)
                        class_start = match.end()
                        build_match = re.search(r'([\w<>,\?]+)\s+build\s*\(', content[class_start:])
                        state_type = build_match.group(1) if build_match else "Unknown"

                        # Extract Methods
                        body = content[class_start:class_start+3000] # Limit lookahead
                        methods = []
                        for m in METHOD_PATTERN.finditer(body):
                            m_name = m.group(1)
                            m_args = m.group(2).strip()
                            m_args = re.sub(r'\s+', ' ', m_args) # Flatten whitespace
                            
                            if m_name not in ['build', 'dispose', 'addListener'] and not m_name.startswith('_'):
                                methods.append(f"{m_name}({m_args})")

                        provider_entries.append({
                            "className": class_name,
                            "type": "Riverpod (Generator)",
                            "state": state_type,
                            "keepAlive": is_keep_alive,
                            "methods": methods
                        })
                    
                    if provider_entries:
                        self.data['providers'][rel_path] = {
                            "imports": self.get_imports(content),
                            "entries": provider_entries
                        }

    # --- Feature: Routes ---
    def scan_routes(self):
        router_path = None
        for root, _, files in os.walk(LIB_DIR):
            for f in files:
                if f in ['app_router.dart', 'router.dart']:
                    router_path = os.path.join(root, f)
                    break
        
        if router_path:
            content = self.read_file(router_path)
            for match in GOROUTE_PATTERN.finditer(content):
                path = match.group(1)
                screen = match.group(2)
                self.data['routing'][path] = screen

    # --- Feature: File Tree ---
    def scan_tree(self):
        file_list = []
        for root, dirs, files in os.walk(LIB_DIR):
            dirs[:] = [d for d in dirs if d not in TREE_IGNORE_DIRS]
            
            for f in files:
                if not any(f.endswith(e) for e in TREE_IGNORE_EXTS):
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, PROJECT_ROOT)
                    
                    # Add tag for UI
                    entry = rel_path
                    if "Screen" in f or "Page" in f:
                        entry += " [UI]"
                    
                    file_list.append(entry)
        
        self.data['files'] = sorted(file_list)

    def run(self):
        print("Scanning project and generating JSON...")
        self.scan_manifest()
        self.scan_routes()
        self.scan_hive_registry_global()
        self.extract_models()
        self.scan_providers()
        self.scan_tree()
        
        with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=2)
        
        print(f"✅ JSON Metadata generated: {OUTPUT_FILE}")

if __name__ == "__main__":
    ProjectScanner().run()
