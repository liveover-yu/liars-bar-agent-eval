# 离线牌桌练习

`offline_liars_bar.py` 是最初的纯 Python 命令行作业，保留它作为语法练习。浏览器版放在同一目录，四位玩家可以通过页面完成一局牌。

## 浏览器版（不调用 LLM API）

原来的 `offline_liars_bar.py` 保留为命令行语法练习。浏览器版把职责拆成：

- `game_engine.py`：发牌、回合、质疑、开枪和胜负规则，相当于 Java 的业务层。
- `web_app.py`：本地 HTTP 接口，相当于 Java 的 Controller。
- `templates/index.html` 与 `static/`：玩家看到并操作的页面。

在本目录打开 PowerShell，先激活你已有的 Python 虚拟环境，再运行：

```powershell
python -m pip install -r requirements.txt
python web_app.py
```

浏览器访问 `http://127.0.0.1:5000`。第一位玩家创建房间，另外三位用房间码入座，P1 等四人到齐后点击“开始牌局”。独自测试时，可以分别在四个独立标签页入座；每个标签页只保存自己的席位凭证。刷新页面不会重置当前牌局。

页面会隐藏手牌，只有玩家主动点击“查看我的手牌”才显示。共用一块屏幕时，请先把设备交给对应玩家，再查看或出牌；这种交接依赖在场玩家配合。使用各自设备时，服务端只向该席位返回自己的手牌，不会把其他人的手牌、未揭示的出牌或子弹位置发给浏览器。

牌局目前只保存在运行中的 Python 进程里；关闭服务后房间会消失。服务默认只监听本机。若要让同一局域网内的其他设备访问，可在启动前设置 `LIARS_BAR_HOST=0.0.0.0`，并使用这台电脑的局域网地址和端口 5000；请勿把这个练习服务直接暴露到互联网。

运行规则与接口测试：

```powershell
python -m unittest discover -s tests -v
```

