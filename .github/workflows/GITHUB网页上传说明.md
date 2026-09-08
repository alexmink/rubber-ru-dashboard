# V6 GitHub 网页一键上传版

## 只需按下面做

1. GitHub 新建一个**空仓库**，例如 `rubber-ru-dashboard`。
2. 解压本 ZIP。
3. 打开解压后的文件夹，按 `Ctrl+A` 全选**里面的所有内容**。
4. GitHub 仓库页面点 `Add file` → `Upload files`。
5. 把刚才全选的内容拖进去。
6. 确认仓库根目录直接出现：
   - `.github`
   - `scripts`
   - `site`
   - `README.md`
   - `requirements.txt`
7. 点 `Commit changes`。
8. `Settings` → `Actions` → `General` → Workflow permissions 选择 `Read and write permissions` → Save。
9. `Settings` → `Pages` → Build and deployment → Source 选择 `GitHub Actions`。
10. `Actions` → 找到更新部署 workflow → `Run workflow` 手动运行一次。
11. 全部绿色后，`Settings` → `Pages` 打开网页。

### 如果 Windows 看不到 `.github`

资源管理器：
`查看` → `显示` → `隐藏的项目`

### 最关键的一点

GitHub 仓库根目录必须直接看到 `.github`，不能变成：

`rubber-ru-dashboard / V6文件夹 / .github`

而应该是：

`rubber-ru-dashboard / .github`
