import ast
import os
import sys

TIMEOUT_SECONDS = 15
USAGE = (
    "用法: scripts/code_ast_reader_cmd.py <file_path> [--no-docstrings] [--private]\n"
    "  <file_path>       : 目標 Python 檔案路徑\n"
    "  --no-docstrings   : 隱藏 Docstring\n"
    "  --private         : 包含私有函式與方法（以 _ 開頭者）"
)


def parse_cli(argv):
    """解析命令列參數"""
    file_path = None
    show_docstrings = True
    show_private = False

    for arg in argv:
        if arg == "--no-docstrings":
            show_docstrings = False
        elif arg == "--private":
            show_private = True
        elif arg.startswith("-"):
            return None, None, None, f"[ERROR] 不支援的選項: {arg}\n{USAGE}"
        elif file_path is None:
            file_path = arg
        else:
            return None, None, None, f"[ERROR] 只能指定一個檔案路徑。\n{USAGE}"

    if not file_path:
        return None, None, None, f"[ERROR] 未提供檔案路徑。\n{USAGE}"

    return file_path, show_docstrings, show_private, None


class CodeASTVisitor(ast.NodeVisitor):
    def __init__(self, show_docstrings=True, show_private=False):
        self.show_docstrings = show_docstrings
        self.show_private = show_private
        self.output = []
        self.indent_level = 0

    def _indent(self):
        return "  " * self.indent_level

    def _format_docstring(self, node):
        if not self.show_docstrings:
            return
        doc = ast.get_docstring(node)
        if doc:
            first_line = doc.strip().split("\n")[0]
            self.output.append(f'{self._indent()}  """{first_line}"""')

    def _format_arg(self, arg):
        arg_str = arg.arg
        if arg.annotation:
            arg_str += f": {ast.unparse(arg.annotation)}"
        return arg_str

    def _format_signature(self, node):
        args = []
        # 位置參數
        for a in node.args.args:
            args.append(self._format_arg(a))
        # 可變參數 *args
        if node.args.vararg:
            args.append(f"*{self._format_arg(node.args.vararg)}")
        # 關鍵字參數 **kwargs
        if node.args.kwarg:
            args.append(f"**{self._format_arg(node.args.kwarg)}")

        returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        return f"({', '.join(args)}){returns}"

    def visit_Module(self, node):
        doc = ast.get_docstring(node)
        if self.show_docstrings and doc:
            first_line = doc.strip().split("\n")[0]
            self.output.append(f'""" 模組說明: {first_line} """\n')

        # 收集模組層級的高級常數/變數指派 (例如 ALL_CAPS = ...)
        constants = []
        for item in node.body:
            if isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        val = ast.unparse(item.value) if hasattr(ast, "unparse") else "..."
                        constants.append(f"{target.id} = {val}")

        if constants:
            self.output.append("常數/組態預設:")
            for c in constants:
                self.output.append(f"  • {c}")
            self.output.append("")

        self.generic_visit(node)

    def visit_ClassDef(self, node):
        if not self.show_private and node.name.startswith("_"):
            return

        bases = [ast.unparse(b) for b in node.bases]
        base_str = f"({', '.join(bases)})" if bases else ""
        
        decorators = [f"@{ast.unparse(d)}" for d in node.decorator_list]
        for dec in decorators:
            self.output.append(f"{self._indent()}{dec}")

        self.output.append(f"{self._indent()}class {node.name}{base_str}:")
        self._format_docstring(node)

        self.indent_level += 1
        self.generic_visit(node)
        self.indent_level -= 1
        self.output.append("")  # 空行分隔類別

    def visit_FunctionDef(self, node):
        self._visit_func(node)

    def visit_AsyncFunctionDef(self, node):
        self._visit_func(node, is_async=True)

    def _visit_func(self, node, is_async=False):
        if not self.show_private and node.name.startswith("_") and not (node.name.startswith("__") and node.name.endswith("__")):
            return

        prefix = "async def" if is_async else "def"
        sig = self._format_signature(node)

        decorators = [f"@{ast.unparse(d)}" for d in node.decorator_list]
        for dec in decorators:
            self.output.append(f"{self._indent()}{dec}")

        self.output.append(f"{self._indent()}{prefix} {node.name}{sig}")
        self._format_docstring(node)


def analyze_ast(file_path, show_docstrings=True, show_private=False):
    if not os.path.exists(file_path):
        return f"[ERROR] 檔案不存在: {file_path}"

    if not file_path.endswith(".py"):
        return f"[ERROR] 目前僅支援 Python 檔案 (.py): {file_path}"

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            code = f.read()
    except Exception as e:
        return f"[ERROR] 無法讀取檔案 '{file_path}': {e}"

    try:
        tree = ast.parse(code, filename=file_path)
    except SyntaxError as se:
        return f"[ERROR] 檔案存在語法錯誤，無法解析 AST:\n  行號 {se.lineno}: {se.msg}\n  內容: {se.text.strip() if se.text else ''}"
    except Exception as e:
        return f"[ERROR] AST 解析失敗: {e}"

    visitor = CodeASTVisitor(show_docstrings=show_docstrings, show_private=show_private)
    visitor.visit(tree)

    result_text = "\n".join(visitor.output).strip()
    if not result_text:
        result_text = "(該檔案未包含任何頂層類別或函式)"

    return f"[PASS] 成功解析 {file_path} 的 AST 結構:\n\n{result_text}"


if __name__ == "__main__":
    try:
        file_path, show_docstrings, show_private, err = parse_cli(sys.argv[1:])
        if err:
            print(err)
            sys.exit(0)
        print(analyze_ast(file_path, show_docstrings, show_private))
    except Exception as e:
        print(f"[ERROR] code_ast_reader 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)