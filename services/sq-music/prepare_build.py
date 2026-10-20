"""Prepare local Java overrides from the audited upstream source.

No Maven build and no runtime dependency additions: javac uses the exact
dependencies already shipped by the fixed upstream image.

This original transformation script is MIT-licensed by this repository.
Generated files contain upstream code; they are ignored and not redistributed.
No license was found at the audited upstream revision; obtaining this source
does not grant redistribution rights. See README.md and SOURCES.md.
"""
from __future__ import annotations
import argparse
import hashlib
from pathlib import Path

UPSTREAM_COMMIT = "cf0f6ed136be0aa13845bbab028c27e404b89d5d"
FILES = {
    "com/sqmusicplus/v3/plug/base/hander/SearchHanderAbstract.java": "4171aab5c07b0db4b22d8bebd1bb79425d2019c0f55b4dbeaf8a45cdf9566538",
    "com/sqmusicplus/v3/controller/TaskController.java": "96983130c4c80aefce618c2919efb367b28c71c816e9acc50cc29f129ae08b21",
}


def replace_one(text: str, before: str, after: str) -> str:
    if text.count(before) != 1:
        raise SystemExit("Upstream structure changed; refusing an imprecise patch")
    return text.replace(before, after, 1)


def logger_without_lombok(text: str, class_name: str) -> str:
    text = replace_one(text, "import lombok.extern.slf4j.Slf4j;\n", "")
    text = replace_one(text, "@Slf4j\n", "")
    declaration = ("public abstract class SearchHanderAbstract implements SearchHander, Serializable {"
                   if class_name == "SearchHanderAbstract" else "public class TaskController {")
    return replace_one(text, declaration, declaration +
        "\n    private static final org.slf4j.Logger log = org.slf4j.LoggerFactory.getLogger(" + class_name + ".class);\n")


def prepare(upstream: Path, destination: Path) -> None:
    for relative, expected in FILES.items():
        source = upstream / "src/main/java" / relative
        data = source.read_bytes()
        if expected and hashlib.sha256(data).hexdigest() != expected:
            raise SystemExit(f"Source checksum differs from audited commit: {relative}")
        text = data.decode("utf-8")
        if "SearchHanderAbstract" in relative:
            text = logger_without_lombok(text, "SearchHanderAbstract")
            text = replace_one(text, '            pathTemplate.put("musicName", baseMusicName);',
                '            pathTemplate.put("taskId", String.valueOf(downloadInfo.getId()));\n'
                '            pathTemplate.put("musicName", baseMusicName);')
            text = replace_one(text,
                '                    log.info("歌曲{}---->已存在不下载", baseMusicName);\n                    return;',
                '                    downloadInfo.setDownloadFile(type.getAbsolutePath());\n'
                '                    log.info("歌曲{}---->已存在不下载", baseMusicName);\n                    return;')
            text = replace_one(text,
                '            throw new RuntimeException("下载失败（标签写入错误）:" + downloadInfo.getDownloadMusicname() + "------->" + e.getMessage(), e);\n        }\n    }',
                '            throw new RuntimeException("下载失败（标签写入错误）:" + downloadInfo.getDownloadMusicname() + "------->" + e.getMessage(), e);\n        }\n'
                '        // Bot bridge: persist only the actual, fully processed file path.\n'
                '        downloadInfo.setDownloadFile(onSuccess.getAbsolutePath());\n    }')
        else:
            text = logger_without_lombok(text, "TaskController")
            text = replace_one(text, '    @Autowired\n    private SqSyncService syncService;',
                '    @Autowired\n    private SqSyncService syncService;\n\n'
                '    /** Private bot bridge. Authentication inherits the normal /api policy. */\n'
                '    @GetMapping("/bot/{id}")\n'
                '    public AjaxResult botTask(@PathVariable("id") Integer id) {\n'
                '        DownloadInfo task = downloadInfoService.getById(id);\n'
                '        if (task == null) return AjaxResult.error("Task not found");\n'
                '        List<DownloadInfo> children = downloadInfoService.list(new LambdaQueryWrapper<DownloadInfo>()\n'
                '                .eq(DownloadInfo::getParentDownloadId, id));\n'
                '        java.util.Map<String,Object> result = new java.util.LinkedHashMap<>();\n'
                '        result.put("task", botTaskSummary(task));\n'
                '        result.put("children", children.stream().map(this::botTaskSummary).toList());\n'
                '        result.put("bridgeVersion", 1);\n'
                '        return AjaxResult.success(result);\n'
                '    }\n\n'
                '    private java.util.Map<String,Object> botTaskSummary(DownloadInfo task) {\n'
                '        java.util.Map<String,Object> result = new java.util.LinkedHashMap<>();\n'
                '        result.put("id", task.getId());\n'
                '        result.put("parentDownloadId", task.getParentDownloadId());\n'
                '        result.put("downloadStatus", task.getDownloadStatus());\n'
                '        result.put("downloadMusicId", task.getDownloadMusicId());\n'
                '        result.put("downloadPlugName", task.getDownloadPlugName());\n'
                '        result.put("downloadBrType", task.getDownloadBrType());\n'
                '        if ("success".equals(task.getDownloadStatus()))\n'
                '            result.put("downloadFile", task.getDownloadFile());\n'
                '        return result;\n'
                '    }')
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("upstream", type=Path)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "patched")
    args = parser.parse_args()
    prepare(args.upstream, args.output)
