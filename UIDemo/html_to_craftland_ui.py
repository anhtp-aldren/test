#!/usr/bin/env python3
"""Compile a small, fixed-layout HTML subset into a Craftland UI asset.

The compiler connects to Craftland Studio's Streamable HTTP MCP endpoint,
creates the UI through the editor, and saves the resulting .ui asset.

Supported input is deliberately narrow: nested div/main/section/span/img,
inline CSS in px, background/color/font/text alignment, and simple CSS grids.
The key coordinate conversion is:

    craftland_y = parent_height - css_top - element_height

HTML is top-left based; Craftland UI's bottom-left anchored transforms are not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


PX = re.compile(r"^(-?[\d.]+)px$")
HEX = re.compile(r"^#[0-9a-fA-F]{3,8}$")
CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)")

# Default skin mapping for UIDemo/popup_shop.html.  New HTML should use <img>
# paths under Assets, which the compiler resolves through Craftland MCP.
DEFAULT_ASSET_MAP = {
    "sk-bg01": "0nhu6y637hkk-mvqenmx0-jq1kp1zecbg",
    "sk-title1": "ppzdinpw76h-mvqenmzi-rrj0yr829c",
    "sk-wb01": "axvhm5gjpfa-mvqennn9-b7gzdkp4egn",
    "sk-wb04": "ix3v18yxtp-mvqennun-m2elq5ewck",
    "sk-filter": "t4uvrtptufb-mvqenn4v-hf96f2nhe0l",
    "sk-check": "u1r8ut8vd5-mvqens09-cwf58u6bv9v",
    "gpopup-close": "z632gv4bcs-mvqenjv0-o7na956k1tc",
    "gbtn-abs": "s2c1p4blx2-mvqenk18-hy2itwg7z08",
}


def px(value: str | None, default: float = 0.0) -> float:
    if not value:
        return default
    match = PX.match(value.strip())
    return float(match.group(1)) if match else default


def color(value: str | None, default: str = "#FFFFFF") -> str:
    """Normalize CSS #RGB because Craftland labels require #RRGGBB."""
    result = (value or default).strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", result):
        return "#" + "".join(component * 2 for component in result[1:])
    return result


def css_url(value: str | None) -> str | None:
    match = CSS_URL.search(value or "")
    return match.group(1) if match else None


def css(style: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for declaration in (style or "").split(";"):
        if ":" not in declaration:
            continue
        key, value = declaration.split(":", 1)
        result[key.strip().lower()] = value.strip()
    if "inset" in result:
        values = result["inset"].split()
        if len(values) == 1:
            result.setdefault("top", values[0])
            result.setdefault("right", values[0])
            result.setdefault("bottom", values[0])
            result.setdefault("left", values[0])
    return result


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    children: list["Node"] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    @property
    def style(self) -> dict[str, str]:
        return css(self.attrs.get("style"))


class TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.root = Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs})
        self.stack[-1].children.append(node)
        if tag not in {"img", "meta", "link", "br", "input"}:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in {"img", "meta", "link", "br", "input"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.stack[-1].text.append(data.strip())


def stable_id(path: str) -> str:
    return hashlib.blake2s(path.encode(), digest_size=6).hexdigest()


class McpClient:
    """Minimal Streamable HTTP MCP client, matching Craftland Studio's endpoint."""
    def __init__(self, url: str) -> None:
        self.url, self.session, self.request_id = url, None, 0

    def post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "MCP-Protocol-Version": "2025-03-26"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                self.session = response.headers.get("Mcp-Session-Id", self.session)
                body = response.read().decode()
        except urllib.error.URLError as error:
            raise RuntimeError(f"Cannot reach Craftland MCP: {error.reason}") from error
        if not body.strip():
            return {}
        if body.lstrip().startswith("event:"):
            body = next((line[5:].strip() for line in body.splitlines() if line.startswith("data:")), "{}")
        return json.loads(body)

    def rpc(self, method: str, params: dict[str, Any]) -> Any:
        self.request_id += 1
        result = self.post({"jsonrpc": "2.0", "id": self.request_id, "method": method, "params": params})
        if "error" in result:
            raise RuntimeError(result["error"].get("message", str(result["error"])))
        return result.get("result")

    def initialize(self) -> None:
        self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "html-to-craftland-ui", "version": "1.0"}})
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict) or result.get("isError"):
            raise RuntimeError(f"Craftland {name} failed: {result}")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        values = []
        for item in result.get("content", []):
            if item.get("type") != "text": continue
            try:
                values.append(json.loads(item["text"]))
            except json.JSONDecodeError:
                values.append(item["text"])
        return values[0] if len(values) == 1 else values


def mcp_url(project_root: Path, override: str | None) -> str:
    if override:
        return override
    with (project_root / ".codex" / "config.toml").open("rb") as stream:
        return tomllib.load(stream)["mcp_servers"]["craftland_studio"]["url"]


def result_id(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("id", "entityId", "assetId", "fileId"):
            if isinstance(value.get(key), str): return value[key]
        for child in value.values():
            try: return result_id(child)
            except LookupError: pass
    if isinstance(value, list):
        for child in value:
            try: return result_id(child)
            except LookupError: pass
    raise LookupError("No id in Craftland response")


def apply_plan(plan: dict[str, Any], client: McpClient, directory: str, name: str) -> str:
    created = client.tool("ui-create", {"directoryPath": directory, "name": name})
    ui_id = result_id(created)
    asset = client.tool("asset-get", {"assetId": ui_id, "withEntities": "true"})
    root_id = next(entity["id"] for entity in asset["entities"] if entity["entityType"] == "UIRootEntity")
    ids = {next(entity["id"] for entity in plan["entities"] if entity["entityType"] == "UIRootEntity"): root_id}
    for entity in plan["entities"]:
        if entity["entityType"] == "UIRootEntity": continue
        transform = entity["transform"]
        # A newly-created Craftland widget is centre-anchored.  Anchors must be
        # changed before its position; otherwise the editor preserves the old
        # screen rectangle while changing anchors and offsets the final result.
        props = {"transform.anchorMin": transform["anchorMin"], "transform.anchorMax": transform["anchorMax"],
                 "transform.pivot": transform["pivot"], "transform.sizeDelta": transform["sizeDelta"],
                 "transform.anchoredPosition": transform["anchoredPosition"], "priority": entity["priority"],
                 "name": entity["name"]}
        if "text" in entity:
            props.update({"text.value": entity["text"]["value"], "text.fontSize": entity["text"]["fontSize"],
                          "text.color": entity["text"]["color"], "text.alignment.h": entity["text"]["alignment"]})
        if "image" in entity:
            props["image.color"] = entity["image"]["color"]
            props["image.fillType"] = entity["image"]["fillType"]
            props["alpha"] = round(entity["image"]["opacity"] * 100)
            source = entity["image"]["source"]
            if source:
                # HTML paths are relative to the document; Craftland resolves project assets by ID.
                source = source.replace("../Assets/", "Assets/")
                props["image.source"] = result_id(client.tool("asset-get", {"assetId": source, "withEntities": "false"}))
        parent_id = ids[entity["parentId"]]
        # The create response includes both an asset ID and a widget ID on some
        # Craftland versions.  Use a temporary unique widget name and search it
        # exactly instead of guessing which ID was returned.
        temporary_name = f"html_{entity['id']}"
        client.tool("asset-entity-create", {"assetId": ui_id, "parentId": parent_id,
                                              "type": entity["entityType"], "name": temporary_name})
        found = client.tool("asset-entity-search", {"assetId": ui_id, "parentId": parent_id,
                                                      "name": temporary_name, "fields": "brief", "pageSize": 1})
        if "results" in found and found["results"]:
            created_id = found["results"][0]["id"]
        elif "headers" in found and "contents" in found and found["contents"]:
            created_id = found["contents"][0][found["headers"].index("id")]
        else:
            raise RuntimeError(f"Could not resolve created UI entity {temporary_name!r}: {found}")
        client.tool("asset-entity-update", {"assetId": ui_id, "entityIds": created_id, "properties": props})
        ids[entity["id"]] = created_id
    client.tool("asset-save", {"assetId": ui_id})
    return ui_id


def text_content(node: Node) -> str:
    return " ".join(node.text).strip()


def style_rules(root: Node) -> dict[str, dict[str, str]]:
    """Read simple tag and class rules from embedded <style> blocks."""
    rules: dict[str, dict[str, str]] = {}

    def visit(node: Node) -> None:
        if node.tag == "style":
            source = re.sub(r"/\*.*?\*/", "", " ".join(node.text), flags=re.S)
            for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", source):
                for selector in match.group(1).split(","):
                    selector = selector.strip()
                    if selector.startswith(".") or selector.isidentifier():
                        rules.setdefault(selector, {}).update(css(match.group(2)))
        for child in node.children:
            visit(child)

    visit(root)
    return rules


def grid_positions(node: Node, style: dict[str, str]) -> dict[int, tuple[float, float]]:
    """Return CSS top-left positions for direct grid children."""
    if style.get("display") != "grid":
        return {}
    columns = re.search(r"repeat\((\d+)\s*,\s*([\d.]+)px\)", style.get("grid-template-columns", ""))
    if not columns:
        return {}
    count, cell_width = int(columns.group(1)), float(columns.group(2))
    row_match = re.search(r"([\d.]+)px", style.get("grid-auto-rows", ""))
    cell_height = float(row_match.group(1)) if row_match else 0.0
    gap = px(style.get("gap"))
    return {
        index: ((index % count) * (cell_width + gap), (index // count) * (cell_height + gap))
        for index, _ in enumerate(node.children)
    }


class Compiler:
    def __init__(self, width: float, height: float, asset_map: dict[str, str],
                 rules: dict[str, dict[str, str]]) -> None:
        self.width = width
        self.height = height
        self.asset_map = asset_map
        self.rules = rules
        self.entities: list[dict[str, Any]] = []

    def computed_style(self, node: Node) -> dict[str, str]:
        result = dict(self.rules.get(node.tag, {}))
        for class_name in node.classes:
            result.update(self.rules.get(f".{class_name}", {}))
        result.update(node.style)
        return result

    @staticmethod
    def node_name(node: Node) -> str:
        """Keep the editor tree recognisable from the HTML source."""
        if node.attrs.get("cname"):
            return node.attrs["cname"]
        if node.attrs.get("id"):
            return node.attrs["id"]
        class_names = node.attrs.get("class", "").split()
        if class_names:
            return " ".join(class_names)
        if node.attrs.get("src"):
            return Path(node.attrs["src"]).stem
        return node.tag

    def emit(self, node: Node, parent_id: str, parent_size: tuple[float, float], path: str,
             grid_offset: tuple[float, float] = (0.0, 0.0),
             flat_parent_id: str | None = None, absolute_offset: tuple[float, float] = (0.0, 0.0),
             flat_canvas_size: tuple[float, float] | None = None,
             button_line: int | None = None) -> None:
        style = self.computed_style(node)
        if node.tag in {"html", "head", "body", "main", "title", "link", "meta", "style"}:
            for index, child in enumerate(node.children):
                self.emit(child, parent_id, parent_size, f"{path}/{child.tag}[{index}]")
            return

        width = px(style.get("width"), parent_size[0])
        height = px(style.get("height"), 0.0)
        left = px(style.get("left")) + grid_offset[0] + absolute_offset[0]
        top = px(style.get("top")) + grid_offset[1] + absolute_offset[1]
        if "sk-tint" in node.classes:
            width, height, left, top = parent_size[0], parent_size[1], 0.0, 0.0
        if "auto" in style.get("margin", "") and "left" not in style:
            left = (parent_size[0] - width) / 2
        if "auto" in style.get("margin", "") and "top" not in style:
            top = (parent_size[1] - height) / 2
        if style.get("inset") == "0":
            width, height = parent_size
            # In a flattened popup, an inset child still covers its CSS parent
            # at that parent's accumulated position.
            left, top = absolute_offset
        if not height and node.tag in {"span", "div"}:
            height = px(style.get("line-height"), px(style.get("font-size"), 20.0))
        if button_line is not None:
            width = parent_size[0]
            height = px(style.get("font-size"), 16.0) * 1.15
            # The mockup uses a vertical, centred flex button label.
            left = 0.0
            top = 3.0 if button_line == 0 else 27.0

        entity_id = stable_id(path)
        classes = node.classes
        direct_text = text_content(node)
        source_path = node.attrs.get("src")
        mask_source = css_url(style.get("-webkit-mask-image") or style.get("mask-image"))
        image_source = self.asset_map.get(source_path or "") or next(
            (self.asset_map[class_name] for class_name in classes if class_name in self.asset_map), source_path or mask_source
        )
        is_text = node.tag == "span" or bool(direct_text)
        is_image = (node.tag == "img" or "sk" in classes or "sk-tint" in classes
                    or "gpopup-close" in classes or "gbtn-abs" in classes or "gmask" in classes)
        entity_type = "UITextEntity" if is_text else "UIImageEntity" if is_image else "UIEmptyEntity"
        name = self.node_name(node)
        # CSS is top-left; Craftland's visual canvas resolves the equivalent
        # bottom-origin coordinate from the element's bottom edge.
        # Button spans are local to their button image; every other popup node
        # uses the shared popup canvas coordinate space.
        coordinate_space = parent_size if button_line is not None else (flat_canvas_size or parent_size)
        craftland_y = coordinate_space[1] - top - height
        # Keep the DOM relationship in Craftland.  Wrapper groups occupy the
        # popup canvas so their descendants may retain their HTML absolute
        # coordinates without changing the validated visual layout.
        target_parent_id = parent_id
        output_position = {"x": left, "y": craftland_y}
        output_size = {"x": width, "y": height}
        if entity_type == "UIEmptyEntity" and flat_canvas_size is not None:
            output_position = {"x": 0, "y": 0}
            output_size = {"x": flat_canvas_size[0], "y": flat_canvas_size[1]}
        entity: dict[str, Any] = {
            "id": entity_id,
            "parentId": target_parent_id,
            "entityType": entity_type,
            "name": name,
            "transform": {
                "anchoredPosition": output_position,
                "sizeDelta": output_size,
                "anchorMin": {"x": 0, "y": 0},
                "anchorMax": {"x": 0, "y": 0},
                "pivot": {"x": 0, "y": 0},
            },
            # Flattened widgets still follow HTML paint order; an explicit z-index
            # is only a local increment.  This keeps labels over their skins.
            "priority": len(self.entities) * 10 + int(float(style.get("z-index", "0"))),
        }
        if entity_type == "UITextEntity":
            entity["text"] = {
                "value": direct_text,
                "fontSize": px(style.get("font-size"), 16),
                "color": color(style.get("color")),
                "alignment": "center" if button_line is not None else style.get("text-align", "left"),
                "lineHeight": px(style.get("line-height"), 0),
            }
        elif entity_type == "UIImageEntity":
            entity["image"] = {
                "source": image_source,
                "color": next((self.computed_style(child).get("background-color")
                               for child in node.children if "sk-tint" in child.classes),
                              color(style.get("background-color"))),
                "opacity": float(style.get("opacity", "1")),
                # The mockup's shared `sk` class denotes a 9-slice panel skin.
                # Plain <img> assets such as item and currency sprites are Simple.
                # Craftland: 0 = Simple; 1 = Sliced / 9-Grid.
                "fillType": 1 if "sk" in classes or "gbtn-abs" in classes else 0,
            }
        self.entities.append(entity)

        # Wrapper nodes retain the HTML tree while sharing the popup canvas.
        starts_flat_canvas = "popup-stage" in classes
        child_flat_parent = entity_id if starts_flat_canvas else flat_parent_id
        # The stage itself is positioned on the HUD; its descendants use local
        # popup coordinates, so do not add the stage's HUD offset to them.
        child_offset = (0.0, 0.0) if starts_flat_canvas else ((left, top) if child_flat_parent else (0.0, 0.0))
        child_canvas_size = (width, height) if starts_flat_canvas else flat_canvas_size
        positions = grid_positions(node, style)
        for index, child in enumerate(node.children):
            if "sk-tint" in child.classes:
                # Tint is folded into its skin image so it remains one widget.
                continue
            self.emit(child, entity_id, (width, height), f"{path}/{child.tag}[{index}]",
                      positions.get(index, (0.0, 0.0)), child_flat_parent, child_offset, child_canvas_size,
                      index if "gbtn-abs" in classes and child.tag == "span" else None)

    def compile(self, root: Node) -> dict[str, Any]:
        root_id = stable_id("root")
        self.entities.append({
            "id": root_id,
            "parentId": None,
            "entityType": "UIRootEntity",
            "name": "Root",
            "transform": {"anchoredPosition": {"x": self.width / 2, "y": self.height / 2},
                          "sizeDelta": {"x": self.width, "y": self.height}},
        })
        for index, child in enumerate(root.children):
            self.emit(child, root_id, (self.width, self.height), f"root/{child.tag}[{index}]")
        return {"format": "craftland-ui-entity-plan/v1", "entities": self.entities}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html", type=Path)
    parser.add_argument("--width", type=float, default=1500)
    parser.add_argument("--height", type=float, default=750)
    parser.add_argument("--ui-name", help="Name for the newly created UI asset")
    parser.add_argument("--ui-directory", default="Assets/UI")
    parser.add_argument("--mcp-url", help="Override the Craftland MCP URL in .codex/config.toml")
    args = parser.parse_args()

    source = TreeBuilder()
    source.feed(args.html.read_text(encoding="utf-8"))
    plan = Compiler(args.width, args.height, DEFAULT_ASSET_MAP, style_rules(source.root)).compile(source.root)
    client = McpClient(mcp_url(Path.cwd(), args.mcp_url))
    client.initialize()
    ui_id = apply_plan(plan, client, args.ui_directory, args.ui_name or args.html.stem)
    print(f"Created and saved Craftland UI {ui_id}")


if __name__ == "__main__":
    main()
