import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts.account_scope import (assert_environment, runtime_data_dir,
                                   scoped_rows, validate_environment_update)

class AccountScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        (self.data / 'account_scope.json').write_text(json.dumps({'environment':'live','history_start':'2026-09-22 21:30:00'}))

    def test_mixed_environment_and_pre_start_records_are_excluded(self):
        good={'environment':'live','status':'closed','close_time':'2026-09-22 22:00:00'}
        rows=[good,{**good,'environment':'demo'},{**good,'environment':''},{**good,'close_time':'2026-09-21 12:00:00'}]
        self.assertEqual(scoped_rows(rows,self.data),[good])

    def test_holdings_survive_baseline_cutoff(self):
        row={'account_mode':'LIVE','status':'holding','open_time':'2026-01-01'}
        self.assertEqual(scoped_rows([row],self.data),[row])

    def test_environment_change_and_removal_rejected(self):
        assert_environment(self.data,'live')
        for vals in ({'ASTRA_OKX_ENV':'demo'},{'OKX_IS_SIMULATED':True}):
            with self.assertRaises(ValueError): validate_environment_update(self.data,vals)
        with self.assertRaises(ValueError): validate_environment_update(self.data,{'ASTRA_OKX_ENV':None},removing=True)
        validate_environment_update(self.data,{'ASTRA_OKX_ENV':'live','LLM_MODEL':'other'})

    def test_bad_scope_fails_closed(self):
        (self.data/'account_scope.json').write_text('{bad')
        with self.assertRaises(ValueError): assert_environment(self.data,'live')

    def test_the_runtime_path_is_fenced_against_the_scope_file(self):
        """**无参**调用才是"这个实例到底用什么环境"的决策点 —— 必须仍被围栏拦住。

        2026-09-26：原用例断言的是"显式传值也被围栏"。那条契约把一个**纯解析**
        函数变成了会读生产磁盘的函数：测试夹具 / 回放 / 离线探针只要构造一个
        demo 环境就会被判违规，实测连带打挂 65 个无关用例（且让沙箱测试读生产）。
        真正需要保护的是"本实例自己的配置"这条路径，也就是无参调用。
        """
        from scripts import okx_runtime
        with patch.object(okx_runtime, 'ROOT', self.root), \
             patch.object(okx_runtime, '_load_dotenv',
                          return_value={'ASTRA_OKX_ENV': 'demo'}):
            with self.assertRaises(ValueError):
                okx_runtime.selected_environment()
        with patch.object(okx_runtime, 'ROOT', self.root), \
             patch.object(okx_runtime, '_load_dotenv',
                          return_value={'ASTRA_OKX_ENV': 'live'}):
            self.assertEqual(okx_runtime.selected_environment().mode, 'live')

    def test_explicit_values_are_a_pure_call_and_never_touch_the_scope_file(self):
        """显式传值是程序化调用（夹具/回放/离线探针），不得读本机 scope。

        这条不是"放松围栏"：真正改配置的写路径（``update_env`` →
        ``validate_environment_update``）与运行路径（无参 ``selected_environment``）
        都仍在围栏内，见本文件其余用例。缺的只是"连问一句都不许"。
        """
        from scripts import okx_runtime
        with patch.object(okx_runtime, 'ROOT', self.root):
            for mode in ('demo', 'live'):
                self.assertEqual(
                    okx_runtime.selected_environment({'ASTRA_OKX_ENV': mode}).mode, mode,
                    f'显式 {mode} 不应被本机 scope(live) 拦住')

    def test_sandbox_redirect_via_env_var_is_honoured(self):
        """`ASTRA_DATA_DIR` 是全仓契约，围栏必须走同一个解析口径。

        该变量由 `tests/config_sandbox.isolate_config` 设置，也被独立部署用来
        迁移 data 目录；factor_library / sync_full_ledger / self_improvement_engine /
        trader.order_lease / trader.position_exit / news_sentiment_harvester 都遵守它。
        围栏此前直读 `ROOT/'data'`（不可重定向的模块级常量），于是沙箱内也去读
        生产 scope —— 这是那 65 个用例的真正死因。
        """
        import os
        with patch.dict(os.environ, {'ASTRA_DATA_DIR': str(self.data)}):
            self.assertEqual(runtime_data_dir(self.root / 'elsewhere'), self.data)

    def test_sandbox_without_scope_file_is_not_fenced_by_production(self):
        """沙箱目录里没有 scope ⇒ 无参调用也不该被生产 scope 拦住。"""
        import os
        empty = self.root / 'sandbox'
        empty.mkdir()
        from scripts import okx_runtime
        with patch.dict(os.environ, {'ASTRA_DATA_DIR': str(empty)}), \
             patch.object(okx_runtime, 'ROOT', self.root), \
             patch.object(okx_runtime, '_load_dotenv',
                          return_value={'ASTRA_OKX_ENV': 'demo'}):
            self.assertEqual(okx_runtime.selected_environment().mode, 'demo')

    def test_runtime_data_dir_defaults_when_env_var_absent(self):
        import os
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ASTRA_DATA_DIR', None)
            self.assertEqual(runtime_data_dir(self.root / 'data'), self.root / 'data')

    def test_unscoped_checkout_retains_legacy_behavior(self):
        rows=[{'environment':'demo'}]
        self.assertEqual(scoped_rows(rows,self.root/'absent'),rows)

if __name__ == '__main__': unittest.main()
