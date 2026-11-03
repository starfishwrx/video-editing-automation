"""Image re-observation for ambiguous local actions; never a full-video pass."""
from pathlib import Path

from ..media import action_frames
from ..media.temporal_samples import validate as validate_samples, guard_outputs
from .. import ai_editor_contract as identity
from ..selection import ai_editor
from .purpose_review import save


def review(samples, output, *, ffmpeg="ffmpeg", ffprobe="ffprobe", font_path):
    from PIL import Image, ImageDraw, ImageFont
    validate_samples(samples)
    guard_outputs(samples,output,(".jpg",".prompt.txt",".request.json",".detail.json"))
    root = Path(output).parent
    metadata = action_frames.probe(samples["source"]["path"],root,ffprobe=ffprobe)
    candidates = samples["frames"]
    if len(candidates) < 2:
        raise ValueError("EXPERIMENT_DETAIL_SOURCE_TOO_SHORT")
    chosen = [candidates[round(i*(len(candidates)-1)/min(15,len(candidates)-1))]
              for i in range(min(16,len(candidates)))]
    indices = [r["source_frame"] for r in chosen]
    images = action_frames.extract(samples["source"]["path"],metadata,indices,root,ffmpeg=ffmpeg,width=270)
    canvas = Image.new("RGB",(1080,4*510),"#222222")
    draw = ImageDraw.Draw(canvas);font=ImageFont.truetype(str(font_path),20)
    for i,(row,image) in enumerate(zip(chosen,images)):
        picture=Image.open(image["path"]).convert("RGB");picture.thumbnail((270,480))
        x,y=i%4*270,i//4*510
        canvas.paste(picture,(x,y+30));draw.text((x+6,y+4),f"frame {row['frame_id']}",font=font,fill="white")
    sheet=Path(output).with_suffix(".jpg");canvas.save(sheet,quality=95)
    prompt="""只看这组真实连续视频的采样图片。先客观描述手里是否拿着物品、物品是什么、怎样接触/离开下方物体。
不要从包装文字或目标推断功能。逐项记录pour倒液、absorb吸收、press纸巾实际接触按压、lift纸巾提起翻转、result纸巾结果外观。
不存在的阶段不要填；手指/硅胶球不能当纸巾。看不清纸巾是否湿润时写看不清，白色不能外推完全无水。
只使用图上存在的frame编号。仅JSON {"observed":"实际事实","phases":[{"kind":"pour|absorb|press|lift|result",
"first_frame":0,"last_frame":1,"observed":"实际发生什么"}],"limitations":"不能确认的内容"}。
这只是补查部分事实，不给完整实验、同源因果或最终视频通过结论。"""
    response=ai_editor._call(prompt,Path(output),sheet)
    valid_ids={r["frame_id"] for r in chosen}
    if (not isinstance(response.get("phases"),list) or not isinstance(response.get("observed"),str)
            or any(not isinstance(p,dict) or type(p.get("first_frame")) is not int
                   or type(p.get("last_frame")) is not int or p["first_frame"] not in valid_ids
                   or p["last_frame"] not in valid_ids or p["first_frame"]>p["last_frame"]
                   or p.get("kind") not in {"pour","absorb","press","lift","result"}
                   or not isinstance(p.get("observed"),str) for p in response["phases"])):
        raise ValueError("EXPERIMENT_DETAIL_FRAME_OR_FACT_INVALID")
    validate_samples(samples)
    artifact={"schema":"experiment-local-frame-detail-v1","response":response,
        "visible_frame_ids":sorted(valid_ids),"model":ai_editor._editor_model(),
        "original":samples["original"],"source":samples["source"],"sheet":identity.source_identity(sheet),
        "complete_experiment_accepted":False,"formal_delivery_allowed":False}
    save(Path(output).with_suffix(".detail.json"),artifact)
    return artifact
