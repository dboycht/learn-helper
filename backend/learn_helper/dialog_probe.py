"""确认弹窗（「当前章节还有任务点未完成」）的专项探针 —— 可独立运行的验证入口。

用法：``py -3.10 -m learn_helper.dialog_probe``（或双击同级的 ``native\\dialog_probe.ps1``）。

**为什么单独一个文件、而不塞进 `verify_backend.py`**：
那套自测跑到相关用例时，沙盒浏览器往往正被前面的用例收掉（`eng.diagnose()` 自己会
`browser.close()`），在那里连 CDP 会 `ECONNREFUSED`。**一个"时绿时红"的测试比没有测试更糟**
（本项目纪律），所以这里的浏览器生命周期完全自管。

**它验证什么**（就是用户报的那个弹窗）：
学习通在"本节还有任务点没做完"时会弹平台自制确认框：
```
提示
当前章节还有任务点未完成，是否去完成？
                     [去学习]  [下一节]
```
它不是浏览器原生 `alert`，`page.on('dialog')` 抓不到。原实现的按钮选择器写死
`.popDiv .nextChapter` / `确定` —— 真实弹窗两颗按钮叫「去学习」「下一节」，
**一个都命中不了** ⇒ 返回 None、弹窗原地不动（用户看到的就是"翻页没反应"）。

fixture 的两个要点（都是为了让这个探针**能失败**）：
1. 类名全是 `zz9 xxx` 之类**毫无提示**的名字，证明新实现不依赖平台 CSS 类名；
2. 页面上**故意**再放一颗同名的「下一节」按钮，用来抓"点错了那一颗"的回归。
"""
import os
import sys
import tempfile

from learn_helper import core

_FIXTURE_MODAL = """<!doctype html><html><head><meta charset="utf-8"><title>LH-DIALOG-PROBE-ONE</title>
<style>
  .page-next { display:block; margin:20px; padding:8px; }
  .mask { position:fixed; inset:0; background:rgba(0,0,0,.4); }
  .zz9.modal-x { position:fixed; left:50%; top:50%; transform:translate(-50%,-50%);
                 width:440px; background:#fff; padding:20px; }
  .zz9 .title { font-size:18px; font-weight:bold; margin-bottom:20px; }
  .zz9 .btns { text-align:right; }
  .zz9 .primary, .zz9 .secondary { display:inline-block; padding:8px 20px; margin-left:10px; }
</style></head>
<body>
  <h1>chapter one</h1>
  <!-- 页面自己的「下一节」：与弹窗里的**同名且可见**。
       天真的"找第一个可见的下一节"会点中它，于是弹窗一直不关。 -->
  <a href="#" class="page-next" onclick="document.title='WRONG:page-button';">下一节</a>
  <div class="mask">
    <div class="zz9 modal-x">
      <div class="title">提示</div>
      <div class="txt">当前章节还有任务点未完成，是否去完成？</div>
      <div class="btns">
        <a href="#" class="zz9 secondary" onclick="document.title='WRONG:go-study';">去学习</a>
        <a href="#" class="zz9 primary" onclick="document.title='OK:dialog-next';">下一节</a>
      </div>
    </div>
  </div>
</body></html>"""

_FIXTURE_PLAIN = """<!doctype html><html><head><meta charset="utf-8"><title>LH-DIALOG-PROBE-PLAIN</title>
</head><body><h1>plain</h1>
<a href="#" onclick="document.title='page-only';">下一节</a></body></html>"""


def main():
    tmp = tempfile.mkdtemp(prefix='lh-dialog-probe-')
    modal = os.path.join(tmp, 'modal.html')
    plain = os.path.join(tmp, 'plain.html')
    with open(modal, 'w', encoding='utf-8') as fh:
        fh.write(_FIXTURE_MODAL)
    with open(plain, 'w', encoding='utf-8') as fh:
        fh.write(_FIXTURE_PLAIN)

    results = []

    def check(name, ok, detail=''):
        results.append((name, bool(ok), detail))
        print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f' -- {detail}' if detail and not ok else ''))

    print('=== 确认弹窗跳过按钮探针 ===')
    ok, _proc = core.kill_and_launch_browser()
    check('沙盒浏览器 9222 就绪', ok, '9222 不可用（先确认本机能连 CDP）')
    if not ok:
        return _summary(results)

    sync_playwright = core.require_playwright()
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(core.CDP_URL, timeout=15000)
            page = browser.contexts[0].new_page()
            try:
                # ---- 有弹窗：必须点中弹窗的「下一节」 ----
                page.goto('file:///' + modal.replace('\\', '/'))
                page.wait_for_load_state('load')
                page.wait_for_timeout(300)

                check('弹窗被识别出来', core.confirmation_dialog_open(page))
                btn, _fr = core.find_confirmation_bypass_button(page)
                check('找到了「跳过」按钮', btn is not None, '返回 None（等于弹窗点不掉）')
                if btn is not None:
                    label = (btn.inner_text() or '').strip()
                    check('挑中的是弹窗的「下一节」而不是「去学习」', label == '下一节', repr(label))
                    btn.click(force=True)
                    page.wait_for_timeout(300)
                    title = page.title()
                    check('确实点中的是弹窗按钮（不是页面自己那颗同名按钮）',
                          title == 'OK:dialog-next', repr(title))

                # ---- 没有弹窗：不许误判（否则会把正常页面当弹窗处理） ----
                page.goto('file:///' + plain.replace('\\', '/'))
                page.wait_for_load_state('load')
                page.wait_for_timeout(300)
                check('无弹窗的普通页面不误判',
                      (not core.confirmation_dialog_open(page))
                      and (core.find_confirmation_bypass_button(page)[0] is None))
            finally:
                page.close()
    except Exception as e:
        check('探针执行', False, f'异常: {e}')
    return _summary(results)


def _summary(results):
    bad = sum(1 for _n, ok, _d in results if not ok)
    print(f'\n{len(results) - bad} passed, {bad} failed')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
