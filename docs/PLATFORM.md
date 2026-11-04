# Cairn 平台架构 / Platform architecture

## 中文

Cairn 将 **作者端** 和 **读者端** 分开。作者端使用画布编辑流程：节点描述处理或学习步骤，连线描述顺序，发布时生成不可变版本。新创建的学习任务固定使用当时选定的版本；已经开始的任务不会因后续发布自动切换。

读者端使用固定页面，按照可执行版本推进上传、裁边、圈选、人工确认及练习等步骤。人工步骤会暂停等待用户完成；失败步骤可在支持的场景下重试。题集可以发布为版本并用于学习流程。

平台的长期方向是让专业学科贡献自己的内容、工具、策略和学习方法。当前可用的是内置节点目录、代码中的工具注册机制和单链路执行器；第三方动态插件、插件市场、分支、循环与任意页面生成尚未实现。添加一种学科通常仍需要开发适配器、注册节点并验证执行能力。

## English

Cairn separates the **authoring** and **learner** experiences. Authors use a canvas to arrange processing and learning nodes. Publishing creates an immutable workflow version; new learning tasks are pinned to the selected version, so later publications do not silently change work already in progress.

The learner application renders fixed pages and advances through supported steps such as upload, cropping, selection, human confirmation, and practice. Human steps pause until completed; supported failed steps can be retried. Collections can be published as versions and used in learning workflows.

The long-term goal is an ecosystem in which subject specialists contribute content sources, tools, strategies, and learning methods. Today, Cairn provides a built-in node catalog, a code-level tool registry, and a single-chain executor. Dynamic third-party plugins, a plugin marketplace, branches, loops, and arbitrary page generation are not implemented. Adding a subject generally still requires development, node registration, and execution validation.
