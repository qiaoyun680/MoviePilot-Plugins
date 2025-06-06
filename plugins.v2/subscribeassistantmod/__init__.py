import json
import random
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional, Union, Callable

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from packaging.version import Version

from app import schemas
from app.chain.storage import StorageChain
from app.chain.subscribe import SubscribeChain
from app.chain.tmdb import TmdbChain
from app.core.config import settings
from app.core.context import MediaInfo, Context, TorrentInfo
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfo
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models import Subscribe, DownloadHistory, TransferHistory

from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.modules.themoviedb import CategoryHelper,TmdbApi
from app.plugins import _PluginBase
from app.schemas import ServiceInfo, TmdbEpisode, TransferInfo
from app.schemas.event import ResourceDownloadEventData, ResourceSelectionEventData, TransferInterceptEventData
from app.schemas.subscribe import Subscribe as SchemaSubscribe
from app.schemas.types import EventType, ChainEventType, MediaType, NotificationType
from app.utils.string import StringUtils
from app.db.subscribe_oper import SubscribeOper
lock = threading.RLock()


class SubscribeAssistantMod(_PluginBase):
    # 插件名称
    plugin_name = "订阅助手魔改版"
    # 插件描述
    plugin_desc = "(基于InfinityPacer原版魔改，增加排除分集类型)多场景管理订阅，实现订阅种子删除以及自动待定/暂停/洗版。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/InfinityPacer/MoviePilot-Plugins/main/icons/subscribeassistant.png"
    # 插件版本
    plugin_version = "2.7.5.1"
    # 插件作者
    plugin_author = "InfinityPacer,Seed680"
    # 作者主页
    author_url = "https://github.com/InfinityPacer"
    # 插件配置项ID前缀
    plugin_config_prefix = "subscribeassistantmod_"
    # 加载顺序
    plugin_order = 10
    # 可使用的用户级别
    auth_level = 1

    # region 私有属性
    tmdb_chain = None
    downloader_helper = None
    downloadhistory_oper = None
    transferhistory_oper = None
    subscribe_oper = None
    # 二级分类
    category: CategoryHelper = None
    # TMDB
    tmdb: TmdbApi = None
    # 是否开启
    _enabled = False
    # 是否发送通知
    _notify = False
    # 是否立即运行一次
    _onlyonce = False
    # 下载检查周期
    _download_check_interval = 5
    # 下载超时自动删除
    _auto_download_delete = False
    # 监听手动删除种子
    _manual_delete_listen = False
    # 监听Tracker响应关键字
    _tracker_response_listen = False
    # Tracker响应关键字
    _tracker_response = None
    # Tracker响应关键字集合
    _tracker_responses = []
    # 删除后触发搜索补全
    _auto_search_when_delete = False
    # 跳过删除记录
    _skip_deletion = True
    # 超时删除时间（小时）
    _download_timeout = 3
    # 超时记录清理时间（小时）
    _timeout_history_cleanup = 24
    # 排除标签
    _delete_exclude_tags = None
    # 自动暂停订阅
    _auto_pause = False
    # 自动暂停的用户
    _auto_pause_user = ""
    # 自动暂停的用户集合
    _auto_pause_users = set()
    # 电影上映后暂停的天数
    _auto_pause_movie_air_days = None
    # 剧集上映后暂停的天数
    _auto_pause_tv_air_days = None
    # 即将播出暂停的天数
    _auto_pause_tv_latest_days = None
    # 无下载时的处理策略
    _auto_pause_no_download_actions = []
    # 电影无下载处理的天数
    _auto_pause_movie_no_download_days = None
    # 剧集无下载处理的天数
    _auto_pause_tv_no_download_days = None
    # 自动待定剧集订阅
    _auto_tv_pending = False
    # 自动待定下载中订阅
    _auto_download_pending = False
    # 剧集待定天数
    _auto_tv_pending_days = 0
    # 剧集待定集数
    _auto_tv_pending_episodes = 0
    # 待定剧集默认总集数
    _auto_update_tv_pending_episodes = 0
    # 元数据检查周期
    _meta_check_interval = None
    # 洗版类型
    _auto_best_type = "no"
    # 洗版类型集合
    _auto_best_types = set()
    # 洗版清理整理记录
    _auto_best_clear_history_type = "no"
    # 洗版清理整理记录集合
    _auto_best_clear_history_types = set()
    # 洗版检查周期
    _auto_best_cron = None
    # 洗版天数
    _auto_best_remaining_days = 60
    # 重置任务
    _reset_task = False
    # 定时器
    _scheduler = None
    # 退出事件
    _event = threading.Event()
    # 分集洗版排除内容类型
    _tv_episode_exclude_type = []
    _all_cat = None

    # endregion

    def init_plugin(self, config: dict = None):
        self.tmdb_chain = TmdbChain()
        self.downloader_helper = DownloaderHelper()
        self.downloadhistory_oper = DownloadHistoryOper()
        self.transferhistory_oper = TransferHistoryOper()
        self.subscribe_oper = SubscribeOper()
        self.category = CategoryHelper()
        self.tmdb = TmdbApi()
        if not config:
            return

        self._enabled = config.get("enabled", False)
        self._notify = config.get("notify", False)
        self._onlyonce = config.get("onlyonce", False)
        self._auto_download_delete = config.get("auto_download_delete", True)
        self._manual_delete_listen = config.get("manual_delete_listen", True)
        self._tracker_response_listen = config.get("tracker_response_listen", True)
        self._tracker_response = config.get("tracker_response") or self.__get_default_tracker_response()
        if self._tracker_response:
            self._tracker_responses = [keyword.strip() for keyword in self._tracker_response.split("\n") if
                                       keyword.strip()]
        else:
            self._tracker_responses = []
        self._tracker_response_listen = config.get("tracker_response_listen", True)
        self._auto_search_when_delete = config.get("auto_search_when_delete", True)
        self._delete_exclude_tags = config.get("delete_exclude_tags", "H&R")
        self._auto_tv_pending = config.get("auto_tv_pending", True)
        self._auto_pause = config.get("auto_pause", True)
        self._meta_check_interval = config.get("meta_check_interval", 6)
        self._auto_download_pending = config.get("auto_download_pending", True)
        self._skip_deletion = config.get("skip_deletion", True)
        self._reset_task = config.get("reset_task", False)
        type_mapping = {
            "tv": {MediaType.TV},
            "tv_episode": {MediaType.TV},
            "movie": {MediaType.MOVIE},
            "all": {MediaType.TV, MediaType.MOVIE}
        }
        self._auto_best_type = config.get("auto_best_type", "no")
        self._auto_best_types = type_mapping.get(self._auto_best_type, set())
        self._auto_best_clear_history_type = config.get("auto_best_clear_history_type", "no")
        self._auto_best_clear_history_types = type_mapping.get(self._auto_best_clear_history_type, set())
        self._auto_best_cron = config.get("auto_best_cron", "0 15 * * *")
        self._download_check_interval = self.__get_float_config(config, "download_check_interval", 5)
        self._download_timeout = self.__get_float_config(config, "download_timeout", 3)
        self._timeout_history_cleanup = self.__get_float_config(config, "timeout_history_cleanup", 0) or None
        self._auto_tv_pending_days = self.__get_float_config(config, "auto_tv_pending_days", 0) or None
        self._auto_tv_pending_episodes = self.__get_float_config(config, "auto_tv_pending_episodes", 0) or None
        self._auto_update_tv_pending_episodes = self.__get_float_config(config, "auto_update_tv_pending_episodes",
                                                                        0) or None
        self._auto_best_remaining_days = self.__get_float_config(config, "auto_best_remaining_days", 0) or None
        self._auto_pause_user = config.get("auto_pause_user")
        self._auto_pause_users = set(self._auto_pause_user.split(",")) if self._auto_pause_user else set()
        self._auto_pause_movie_air_days = self.__get_float_config(config, "auto_pause_movie_air_days",
                                                                  0) or None
        self._auto_pause_tv_air_days = self.__get_float_config(config, "auto_pause_tv_air_days", 0) or None
        self._auto_pause_tv_latest_days = self.__get_float_config(config, "auto_pause_tv_latest_days",
                                                                  0) or None
        self._auto_pause_no_download_actions = config.get("auto_pause_no_download_actions", [])
        self._auto_pause_movie_no_download_days = self.__get_float_config(config, "auto_pause_movie_no_download_days",
                                                                          0) or None
        self._auto_pause_tv_no_download_days = self.__get_float_config(config, "auto_pause_tv_no_download_days",
                                                                       0) or None
        self._tv_episode_exclude_type = config.get("tv_episode_exclude_type", [])
        self._all_cat = [*self.category.tv_categorys, *self.category.movie_categorys]

        # 停止现有任务
        self.stop_service()

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        self._scheduler.start()
        if self._reset_task:
            logger.info("订阅助手服务，即将开始重置任务")
            self._scheduler.add_job(
                func=self.reset_task,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="订阅助手",
            )
            self._reset_task = False

        if self._onlyonce:
            logger.info("订阅助手服务，立即运行一次")
            self._scheduler.add_job(
                func=self.auto_check,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                name="订阅助手",
            )
            self._onlyonce = False

        self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/subscribe_toggle",
            "event": EventType.PluginAction,
            "desc": "切换订阅状态",
            "category": "订阅",
            "data": {
                "action": "subscribe_toggle"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        _all_cat = [{"title": d, "value": d} for d in [*self.category.tv_categorys, *self.category.movie_categorys]]
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                            'hint': '开启后插件将处于激活状态',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                            'hint': '是否在特定事件发生时发送通知',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'reset_task',
                                            'label': '重置数据',
                                            'hint': '将重置所有待定订阅及清理相关任务',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                            'hint': '插件将立即运行一次',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'download_check_interval',
                                            'label': '下载检查周期',
                                            'items': [
                                                {'title': '5分钟', 'value': 5},
                                                {'title': '10分钟', 'value': 10},
                                                {'title': '15分钟', 'value': 15},
                                                {'title': '30分钟', 'value': 30},
                                                {'title': '60分钟', 'value': 60},
                                                {'title': '120分钟', 'value': 120},
                                            ],
                                            'hint': '下载检查的周期，定时检查下载任务状态',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'meta_check_interval',
                                            'label': '元数据检查周期',
                                            'items': [
                                                {'title': '1小时', 'value': 1},
                                                {'title': '3小时', 'value': 3},
                                                {'title': '6小时', 'value': 6},
                                                {'title': '12小时', 'value': 12},
                                                {'title': '24小时', 'value': 24}
                                            ],
                                            'item-value': 'value',  # 关键属性
                                            'item-title': 'title',  # 显示的文本字段
                                            'hint': '元数据检查的周期，定时检查订阅元数据状态',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'auto_best_cron',
                                            'label': '洗版检查周期',
                                            'hint': '洗版检查的周期，如 0 15 * * *',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VTabs',
                        'props': {
                            'model': '_tabs',
                            'style': {
                                'margin-top': '8px',
                                'margin-bottom': '16px'
                            },
                            'stacked': True,
                            'fixed-tabs': True
                        },
                        'content': [
                            {
                                'component': 'VTab',
                                'props': {
                                    'value': 'delete_tab'
                                },
                                'text': '种子删除'
                            },
                            {
                                'component': 'VTab',
                                'props': {
                                    'value': 'pending_tab'
                                },
                                'text': '订阅待定'
                            },
                            {
                                'component': 'VTab',
                                'props': {
                                    'value': 'pause_tab'
                                },
                                'text': '订阅暂停'
                            },
                            {
                                'component': 'VTab',
                                'props': {
                                    'value': 'best_tab'
                                },
                                'text': '订阅洗版'
                            }
                        ]
                    },
                    {
                        'component': 'VWindow',
                        'props': {
                            'model': '_tabs'
                        },
                        'content': [
                            {
                                'component': 'VWindowItem',
                                'props': {
                                    'value': 'delete_tab'
                                },
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'props': {
                                            'style': {
                                                'margin-top': '0px'
                                            }
                                        },
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'auto_download_delete',
                                                            'label': '下载超时自动删除',
                                                            'hint': '订阅下载超时将自动删除种子',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'manual_delete_listen',
                                                            'label': '监听手动删除种子',
                                                            'hint': '监听用户手动删除的种子记录',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'tracker_response_listen',
                                                            'label': '监听Tracker响应关键字',
                                                            'hint': '命中Tracker响应关键字时将自动删除种子',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'open_tracker_dialog',
                                                            'label': '打开Tracker配置窗口',
                                                            'hint': '自定义Tracker配置以实现更精准的种子匹配',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'auto_search_when_delete',
                                                            'label': '删除后触发搜索补全',
                                                            'hint': '种子删除后将自动触发搜索补全',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'skip_deletion',
                                                            'label': '跳过种子删除记录',
                                                            'hint': '跳过最近删除的种子，避免再次下载',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'download_timeout',
                                                            'label': '下载超时时间',
                                                            'type': 'number',
                                                            "min": "0",
                                                            'hint': 'N小时内未完成下载任务视为超时',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'timeout_history_cleanup',
                                                            'label': '种子删除记录清理时间',
                                                            'type': 'number',
                                                            "min": "0",
                                                            'hint': '定时清理N小时前的种子删除记录',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'delete_exclude_tags',
                                                            'label': '排除标签',
                                                            'hint': '需要排除的标签，多个标签用逗号分隔',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'VWindowItem',
                                'props': {
                                    'value': 'pending_tab'
                                },
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'props': {
                                            'style': {
                                                'margin-top': '0px'
                                            }
                                        },
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 6
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'auto_download_pending',
                                                            'label': '自动待定下载中订阅',
                                                            'hint': '自动标记正在下载的订阅为待定状态，避免提前完成订阅',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 6
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'auto_tv_pending',
                                                            'label': '自动待定剧集订阅',
                                                            'hint': '自动标记订阅剧集为待定状态，避免提前完成订阅',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'auto_tv_pending_days',
                                                            'label': '剧集待定天数',
                                                            'type': 'number',
                                                            "min": "0",
                                                            'hint': '当前日期小于上映日期加N天，则视为待定，为空时不处理',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'auto_tv_pending_episodes',
                                                            'label': '剧集待定集数',
                                                            'type': 'number',
                                                            "min": "0",
                                                            'hint': '剧集数小于等于设置的集数，则视为待定，为空时不处理',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'auto_update_tv_pending_episodes',
                                                            'label': '待定剧集默认总集数',
                                                            'type': 'number',
                                                            "min": "0",
                                                            'hint': '待定剧集的默认总集数，用于支持订阅信息未更新时正常下载，为空时不处理',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'VWindowItem',
                                'props': {
                                    'value': 'pause_tab'
                                },
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'props': {
                                            'style': {
                                                'margin-top': '0px'
                                            }
                                        },
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSwitch',
                                                        'props': {
                                                            'model': 'auto_pause',
                                                            'label': '自动暂停订阅',
                                                            'hint': '自动标记订阅为暂停状态，避免无意义的请求',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 8
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'auto_pause_user',
                                                            'label': '自动暂停新增订阅的用户',
                                                            'hint': '用户新增订阅时将自动暂停，多个用户使用逗号分隔',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'auto_pause_movie_air_days',
                                                            'label': '电影上映暂停天数',
                                                            'type': 'number',
                                                            "min": "0",
                                                            'hint': '当前日期小于上映日期减N天，则视为暂停，为空时不处理',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'auto_pause_tv_air_days',
                                                            'label': '剧集上映暂停天数',
                                                            'type': 'number',
                                                            "min": "0",
                                                            'hint': '当前日期小于上映日期减N天，则视为暂停，为空时不处理',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'auto_pause_tv_latest_days',
                                                            'label': '即将播出暂停天数',
                                                            'type': 'number',
                                                            "min": "0",
                                                            'hint': '已存在最新播出集，且下集日期距离当前日期大于N天，则视为暂停，为空时不处理',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'auto_pause_movie_no_download_days',
                                                            'label': '电影无下载处理天数',
                                                            'type': 'number',
                                                            "min": "0",
                                                            'hint': '电影上映后，N天内无新的订阅下载，则自动处理，为空时不处理',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'auto_pause_tv_no_download_days',
                                                            'label': '剧集无下载处理天数',
                                                            'type': 'number',
                                                            "min": "0",
                                                            'hint': '剧集上映后，N天内无新的订阅下载，则自动处理，为空时不处理',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'multiple': True,
                                                            'chips': True,
                                                            'clearable': True,
                                                            'model': 'auto_pause_no_download_actions',
                                                            'label': '无下载处理策略',
                                                            'hint': '选择无下载时的处理策略',
                                                            'persistent-hint': True,
                                                            'items': [
                                                                {'title': '暂停电影订阅', 'value': 'pause_movie'},
                                                                {'title': '暂停剧集订阅', 'value': 'pause_tv'},
                                                                {'title': '完成电影订阅', 'value': 'complete_movie'},
                                                                {'title': '完成剧集订阅', 'value': 'complete_tv'},
                                                                {'title': '删除电影订阅', 'value': 'delete_movie'},
                                                                {'title': '删除剧集订阅', 'value': 'delete_tv'}
                                                            ],
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                'component': 'VWindowItem',
                                'props': {
                                    'value': 'best_tab'
                                },
                                'content': [
                                    {
                                        'component': 'VRow',
                                        'props': {
                                            'style': {
                                                'margin-top': '0px'
                                            }
                                        },
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'model': 'auto_best_type',
                                                            'label': '洗版类型',
                                                            'items': [
                                                                {'title': '全部', 'value': 'all'},
                                                                {'title': '关闭', 'value': 'no'},
                                                                {'title': '电影', 'value': 'movie'},
                                                                {'title': '剧集', 'value': 'tv'},
                                                                {'title': '剧集（分集下载）', 'value': 'tv_episode'}
                                                            ],
                                                            'hint': '选择需要自动洗版的类型',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'model': 'auto_best_clear_history_type',
                                                            'label': '清理整理记录（实验性功能）',
                                                            'items': [
                                                                {'title': '全部', 'value': 'all'},
                                                                {'title': '关闭', 'value': 'no'},
                                                                {'title': '电影', 'value': 'movie'},
                                                                {'title': '剧集', 'value': 'tv'},
                                                            ],
                                                            'hint': '洗版下载时，将清理相关的整理记录并删除源文件及媒体库文件',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            },
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VTextField',
                                                        'props': {
                                                            'model': 'auto_best_remaining_days',
                                                            'label': '洗版时限',
                                                            'type': 'number',
                                                            "min": "1",
                                                            'hint': '达到指定天数后自动终止洗版，若有下载则按最新时间计算，为空时不处理',
                                                            'persistent-hint': True
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'VRow',
                                        'props': {
                                            'style': {
                                                'margin-top': '0px',

                                            }
                                        },
                                        'content': [
                                            {
                                                'component': 'VCol',
                                                'props': {
                                                    'cols': 12,
                                                    'md': 4
                                                    #'show': '{{auto_best_type == `tv_episode` || auto_best_type == `all`}}'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'VSelect',
                                                        'props': {
                                                            'multiple': True,
                                                            'model': 'tv_episode_exclude_type',
                                                            'label': '分集排除内容类型',
                                                            'items': _all_cat,
                                                            'hint': '选择分集需要排除自动洗版的内容类型',
                                                            'persistent-hint': True,
                                                        }
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'props': {
                            'style': {
                                'margin-top': '12px'
                            },
                        },
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '注意：本插件仅支持 TMDB 数据源，相关订阅状态说明，请查阅 '
                                        },
                                        'content': [
                                            {
                                                'component': 'a',
                                                'props': {
                                                    'href': 'https://github.com/jxxghp/MoviePilot/pull/3330',
                                                    'target': '_blank'
                                                },
                                                'content': [
                                                    {
                                                        'component': 'u',
                                                        'text': '#3330'
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'error',
                                            'variant': 'tonal',
                                            'text': '注意：本插件可能导致订阅数据异常，媒体文件丢失，相关风险请自行评估与承担'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        "component": "VDialog",
                        "props": {
                            "model": "open_tracker_dialog",
                            "max-width": "65rem",
                            "overlay-class": "v-dialog--scrollable v-overlay--scroll-blocked",
                            "content-class": "v-card v-card--density-default v-card--variant-elevated rounded-t"
                        },
                        "content": [
                            {
                                "component": "VCard",
                                "props": {
                                    "title": "自定义Tracker配置"
                                },
                                "content": [
                                    {
                                        "component": "VDialogCloseBtn",
                                        "props": {
                                            "model": "open_tracker_dialog"
                                        }
                                    },
                                    {
                                        "component": "VCardText",
                                        "props": {},
                                        "content": [
                                            {
                                                'component': 'VRow',
                                                'content': [
                                                    {
                                                        'component': 'VCol',
                                                        'props': {
                                                            'cols': 12,
                                                        },
                                                        'content': [
                                                            {
                                                                'component': 'VTextarea',
                                                                'props': {
                                                                    'model': 'tracker_response',
                                                                    'label': 'Tracker响应关键字',
                                                                    'rows': 10,
                                                                    'hint': '每一行一个关键字，忽略大小写，支持正则表达式匹配',
                                                                    'persistent-hint': True
                                                                }
                                                            }
                                                        ]
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "download_check_interval": 5,
            "auto_download_delete": True,
            "manual_delete_listen": True,
            "auto_search_when_delete": True,
            "skip_deletion": True,
            "download_timeout": 3,
            "timeout_history_cleanup": 24,
            "delete_exclude_tags": "H&R",
            "auto_pause": True,
            "auto_pause_movie_air_days": 7,
            "auto_pause_tv_air_days": 7,
            "auto_pause_tv_latest_days": 30,
            "auto_pause_no_download_actions": [],
            "auto_pause_movie_no_download_days": 180,
            "auto_pause_tv_no_download_days": 90,
            "auto_tv_pending": True,
            "auto_download_pending": True,
            "auto_tv_pending_episodes": 1,
            "auto_update_tv_pending_episodes": 99,
            "meta_check_interval": 6,
            "auto_best_type": "no",
            "auto_best_clear_history_type": "no",
            "auto_best_cron": "0 15 * * *",
            "tracker_response": self.__get_default_tracker_response(),
            "tracker_response_listen": True,
            "tv_episode_exclude_type": []
        }

    def get_page(self) -> List[dict]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if not self._enabled:
            return []

        services = []
        if self._download_check_interval and (
                self._auto_download_delete or self._manual_delete_listen or
                self._tracker_response_listen or self._auto_download_pending):
            services.append({
                "id": f"{self.__class__.__name__}_download",
                "name": f"下载检查",
                "trigger": "interval",
                "func": self.download_check,
                "kwargs": {"minutes": self._download_check_interval}
            })
        if self._meta_check_interval:
            services.append({
                "id": f"{self.__class__.__name__}_meta_check",
                "name": f"元数据检查",
                "trigger": "interval",
                "func": self.meta_check,
                "kwargs": {"hours": self._meta_check_interval}
            })
        if self._auto_best_type != "no" and self._auto_best_cron:
            services.append({
                "id": f"{self.__class__.__name__}_best_version",
                "name": f"洗版检查",
                "trigger": CronTrigger.from_crontab(self._auto_best_cron),
                "func": self.best_version_check,
                "kwargs": {}
            })
        return services

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))

    @staticmethod
    def __get_float_config(config: dict, key: str, default: float) -> float:
        """
        获取int配置项
        """
        try:
            return float(config.get(key, default))
        except (ValueError, TypeError):
            return default

    def __update_config(self):
        """
        更新配置
        """
        config = {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "download_check_interval": self._download_check_interval,
            "auto_download_delete": self._auto_download_delete,
            "manual_delete_listen": self._manual_delete_listen,
            "tracker_response_listen": self._tracker_response_listen,
            "tracker_response": self._tracker_response,
            "auto_search_when_delete": self._auto_search_when_delete,
            "delete_exclude_tags": self._delete_exclude_tags,
            "auto_tv_pending": self._auto_tv_pending,
            "meta_check_interval": self._meta_check_interval,
            "auto_download_pending": self._auto_download_pending,
            "auto_best_cron": self._auto_best_cron,
            "auto_best_type": self._auto_best_type,
            "auto_best_clear_history_type": self._auto_best_clear_history_type,
            "skip_deletion": self._skip_deletion,
            "download_timeout": self._download_timeout,
            "timeout_history_cleanup": self._timeout_history_cleanup,
            "auto_tv_pending_days": self._auto_tv_pending_days,
            "auto_tv_pending_episodes": self._auto_tv_pending_episodes,
            "auto_update_tv_pending_episodes": self._auto_update_tv_pending_episodes,
            "auto_best_remaining_days": self._auto_best_remaining_days,
            "reset_task": self._reset_task,
            "auto_pause": self._auto_pause,
            "auto_pause_user": self._auto_pause_user,
            "auto_pause_movie_air_days": self._auto_pause_movie_air_days,
            "auto_pause_tv_air_days": self._auto_pause_tv_air_days,
            "auto_pause_tv_latest_days": self._auto_pause_tv_latest_days,
            "auto_pause_no_download_actions": self._auto_pause_no_download_actions,
            "auto_pause_movie_no_download_days": self._auto_pause_movie_no_download_days,
            "auto_pause_tv_no_download_days": self._auto_pause_tv_no_download_days,
            "tv_episode_exclude_type": self._tv_episode_exclude_type,
        }
        self.update_config(config=config)

    def reset_task(self):
        """
        重置任务
        """
        subscribes = self.subscribe_oper.list("P")
        logger.info(f"开始重置任务，共有 {len(subscribes)} 个待定订阅任务")
        for subscribe in subscribes:
            self.subscribe_oper.update(sid=subscribe.id, payload={"state": "R", "manual_total_episode": 0})
            logger.info(f"待定订阅 {self.__format_subscribe(subscribe)} 已重置订阅状态为 R，手动更新集数状态为 False")
        SubscribeChain().check()

        self.__save_data("subscribes", {})
        self.__save_data("torrents", {})
        self.__save_data("deletes", {})
        self.__save_data("states", {})
        logger.info("已重置所有订阅任务、下载种子任务和超时删除记录")

    def auto_check(self):
        """
        订阅自动检查
        """
        self.meta_check()
        self.download_check()
        self.best_version_check()

    def download_check(self):
        """
        下载检查
        """
        if not (self._auto_download_delete or self._manual_delete_listen or
                self._tracker_response_listen or self._auto_download_pending):
            return

        logger.info("开始清理超时种子记录...")
        self.process_delete_task()
        logger.info("超时种子记录清理完成...")

        logger.info("开始检查下载种子任务...")
        self.process_download_task()
        logger.info("下载种子任务检查完成...")

    def meta_check(self):
        """
        元数据检查
        """
        if not (self._auto_tv_pending or self._auto_pause):
            return

        logger.info("开始检查订阅暂停...")
        self.process_subscribe_pause()
        logger.info("订阅暂停检查完成...")

        logger.info("开始检查剧集待定...")
        self.process_tv_pending()
        logger.info("剧集待定检查完成...")

    def best_version_check(self):
        """
        洗版检查
        """
        subscribes = self.subscribe_oper.list(state="N,R,P")
        if not subscribes:
            return

        logger.info("开始检查订阅洗版...")
        self.process_best_version_complete(subscribes)
        logger.info("订阅洗版检查完成...")

    @eventmanager.register(EventType.PluginAction)
    def toggle_subscribe_state(self, event: Event = None):
        """
        切换订阅状态
        """
        if not event:
            return
        event_data = event.event_data
        if not event_data or event_data.get("action") != "subscribe_toggle":
            return
        subscribe_keyword = event_data.get("arg_str")
        if not subscribe_keyword:
            self.post_message(channel=event_data.get("channel"), title="未能获取到订阅信息",
                              userid=event_data.get("user"), source=event_data.get("source"))
            return

        subscribes = self.subscribe_oper.list()
        if not subscribes:
            self.post_message(channel=event_data.get("channel"), title="没有找到符合要求的订阅",
                              userid=event_data.get("user"), source=event_data.get("source"))
            return

        if StringUtils.is_number(subscribe_keyword):
            filtered_subscribes = [s for s in subscribes if s.id == int(subscribe_keyword)]
        else:
            filtered_subscribes = [s for s in subscribes if s.name == subscribe_keyword]

        if not filtered_subscribes:
            self.post_message(channel=event.event_data.get("channel"), title=self.plugin_name,
                              text="没有找到符合要求的订阅",
                              userid=event_data.get("user"), source=event_data.get("source"))
            return

        if len(filtered_subscribes) == 1:
            subscribe = filtered_subscribes[0]
            state = "S" if subscribe.state != "S" else "R"
            self.subscribe_oper.update(sid=subscribe.id, payload={"state": state})
            status = "禁用" if state == "S" else "启用"
            self.post_message(channel=event.event_data.get("channel"),
                              title=f"{self.__format_subscribe(subscribe=subscribe)} 已{status}",
                              userid=event_data.get("user"), source=event_data.get("source"))
        else:
            title = f"共有 {len(subscribes)} 个订阅，回复对应指令操作： " \
                    f"\n- 切换订阅状态：/subscribe_toggle [id]"
            messages = []
            for subscribe in filtered_subscribes:
                if subscribe.type == MediaType.MOVIE.value:
                    messages.append(f"{subscribe.id}. {subscribe.name}（{subscribe.year}）")
                else:
                    messages.append(f"{subscribe.id}. {subscribe.name}（{subscribe.year}）第{subscribe.season}季 ")
            self.post_message(channel=event.event_data.get("channel"), title=title, text="\n".join(messages),
                              userid=event_data.get("user"), source=event_data.get("source"))

    @eventmanager.register(EventType.SubscribeDeleted)
    def handle_subscribe_deleted_event(self, event: Event = None):
        """
        处理订阅删除事件
        """
        try:
            # 验证事件数据
            if not event or not event.event_data:
                return

            subscribe_id = event.event_data.get("subscribe_id")
            subscribe_dict = event.event_data.get("subscribe_info")
            logger.debug(f"接收到订阅删除事件，订阅 ID: {subscribe_id}，数据：{subscribe_dict}")
            self.clear_tasks(subscribe_id=subscribe_id, subscribe=subscribe_dict)
        except Exception as e:
            logger.error(f"处理订阅删除事件时发生错误: {str(e)}", exc_info=True)

    @eventmanager.register(EventType.SubscribeAdded)
    def handle_subscribe_added_event(self, event: Event = None):
        """
        处理订阅添加事件
        """
        try:
            # 验证事件数据
            if not event or not event.event_data:
                return

            # 自动待定/暂停功能未开启
            if not (self._auto_tv_pending or self._auto_pause):
                logger.debug("自动待定/暂停功能未开启，跳过处理")
                return

            subscribe_id = event.event_data.get("subscribe_id")

            username = event.event_data.get("username")
            mediainfo_dict = event.event_data.get("mediainfo")
            logger.debug(f"接收到订阅添加事件，来自用户: {username}, 订阅 ID: {subscribe_id}, 数据: {mediainfo_dict}")

            # 缺少订阅信息或媒体信息
            if not subscribe_id or not mediainfo_dict:
                logger.warning(f"订阅事件数据缺失，跳过处理。订阅 ID: {subscribe_id}, 媒体信息: {mediainfo_dict}")
                return

            # 获取订阅信息和媒体信息
            subscribe = self.subscribe_oper.get(subscribe_id)
            mediainfo = MediaInfo()
            mediainfo.from_dict(mediainfo_dict)

            # 洗版订阅跳过处理
            if subscribe.best_version:
                logger.debug(f"{self.__format_subscribe(subscribe)} 为洗版订阅，跳过处理")
                return

            # 订阅或媒体信息获取失败
            if not subscribe or not mediainfo:
                logger.error(f"订阅 ID {subscribe_id} 的订阅信息获取失败，媒体标题: {mediainfo_dict.get('title_year')}",
                             exc_info=True)
                return

            # 调用公共方法处理订阅
            self.process_subscribe_pause_for_user(subscribe_id=subscribe_id)
            self.process_subscribe_pause(subscribe_id=subscribe_id)
            self.process_tv_pending(subscribe_id=subscribe_id)
        except Exception as e:
            logger.error(f"处理订阅添加事件时发生错误: {str(e)}", exc_info=True)

    @eventmanager.register(EventType.SubscribeModified)
    def handle_subscribe_modified_event(self, event: Event = None):
        """
        处理订阅更新事件
        """
        try:
            # 验证事件数据
            if not event or not event.event_data:
                return

            subscribe_id = event.event_data.get("subscribe_id")
            subscribe_info = event.event_data.get("subscribe_info") or {}
            old_subscribe_info = event.event_data.get("old_subscribe_info") or {}

            logger.debug(f"接收到订阅更新事件，来自用户: {subscribe_info.get('username')}, 订阅 ID: {subscribe_id}")

            # 缺少订阅信息
            if not subscribe_id or not subscribe_info or not old_subscribe_info:
                logger.warning(f"订阅事件数据缺失，跳过处理。订阅 ID: {subscribe_id}")
                return

            # 获取订阅信息
            subscribe = self.subscribe_oper.get(subscribe_id)

            # 比对差异
            different_keys = {
                key for key in subscribe_info.keys() & old_subscribe_info.keys()
                if subscribe_info[key] != old_subscribe_info[key]
            }

            # 重置订阅状态
            self.__with_lock_and_update_subscribe_tasks(method=self.__reset_subscribe_task_state_when_updated,
                                                        subscribe=subscribe, different_keys=different_keys)
        except Exception as e:
            logger.error(f"处理订阅更新事件时发生错误: {str(e)}", exc_info=True)

    @eventmanager.register(EventType.SubscribeComplete)
    def handle_subscribe_complete_event(self, event: Event = None):
        """
        处理订阅完成事件
        """
        try:
            # 验证事件数据
            if not event or not event.event_data:
                return

            subscribe_id = event.event_data.get("subscribe_id")
            subscribe_dict = event.event_data.get("subscribe_info")
            mediainfo_dict = event.event_data.get("mediainfo")

            logger.debug(f"接收到订阅完成事件，订阅数据：{subscribe_dict}，媒体数据：{mediainfo_dict}")

            # 订阅完成清理订阅任务数据
            self.clear_tasks(subscribe_id=subscribe_id, subscribe=subscribe_dict)

            if not self._auto_best_types:
                logger.debug("自动洗版功能未开启，跳过处理")
                return

            # 缺少订阅信息或媒体信息
            if not subscribe_dict or not mediainfo_dict:
                logger.warning(f"订阅事件数据缺失，跳过处理。订阅数据: {subscribe_dict}, 媒体信息: {mediainfo_dict}")
                return

            # 判断剧集分集类型是否排除
            if self._tv_episode_exclude_type:
                logger.debug(f"剧集分集类型排除已设置，跳过类别:{self._tv_episode_exclude_type}")
                if "电视剧" == mediainfo_dict["type"]:
                    mtype = MediaType.TV
                else:
                    mtype = MediaType.MOVIE
                info = self.tmdb.get_info(mtype=mtype,
                                            tmdbid=mediainfo_dict["tmdb_id"])
                cat = None
                if info:
                    # 确定二级分类
                    if info.get('media_type') == MediaType.TV:
                        cat = self.category.get_tv_category(info)
                    else:
                        cat = self.category.get_movie_category(info)
                else:
                    logger.warn(f'{mediainfo_dict["title"]} 未获取到tmdb信息')

                if cat:
                    logger.debug(f'本剧集类别:{cat}')
                    if str(cat) in self._tv_episode_exclude_type:
                        logger.debug(f"剧集分集类型{str(cat)}被排除，跳过自动洗版处理")
                        return
                else:
                    logger.warn(f'{mediainfo_dict["title"]} 未获取到二级分类信息')
            mediainfo = MediaInfo()
            mediainfo.from_dict(mediainfo_dict)

            # 调用公共方法处理订阅
            self.process_best_version(subscribe_dict=subscribe_dict, mediainfo=mediainfo)
        except Exception as e:
            logger.error(f"处理订阅完成事件时发生错误: {str(e)}", exc_info=True)

    @eventmanager.register(EventType.DownloadAdded)
    def handle_download_added_event(self, event: Event = None):
        """
        处理下载添加事件
        """
        try:
            # 验证事件数据
            if not event or not event.event_data:
                return

            # 下载超时删除/监听手动删除/监听Tracker响应关键字/下载自动待定功能未开启
            if not (self._auto_download_delete or self._manual_delete_listen or
                    self._tracker_response_listen or self._auto_download_pending):
                logger.debug("下载超时删除/监听手动删除/监听Tracker响应关键字/下载自动待定功能未开启，跳过处理")
                return

            torrent_hash = event.event_data.get("hash")
            context: Context = event.event_data.get("context")
            downloader = event.event_data.get("downloader")
            episodes = list(event.event_data.get("episodes", []))
            username = event.event_data.get("username")
            source = event.event_data.get("source")

            logger.debug(f"接收到下载添加事件，来自用户: {username}, 数据: {event.event_data}")

            subscribe_info, subscribe = self.__get_subscribe_by_source(source=source)
            if not subscribe_info or not subscribe:
                logger.debug(f"未能找到订阅信息，跳过处理")
                return

            service = self.__get_downloader_service(downloader=downloader)
            if not service:
                logger.info(f"触发添加下载事件，但没有获取到下载器 {downloader} 服务，跳过处理")
                return

            if not torrent_hash or not context or not context.torrent_info:
                logger.info("没有获取到有效的种子任务信息，跳过处理")
                return

            torrent = self.__get_torrents(downloader=service.instance, torrent_hashes=torrent_hash)
            if not torrent:
                logger.info(f"没有在下载器中获取到 {torrent_hash} 种子信息，跳过处理")
                return

            # 更新订阅下载任务
            self.__with_lock_and_update_subscribe_tasks(method=self.__update_subscribe_torrent_task,
                                                        subscribe=subscribe,
                                                        torrent_hash=torrent_hash,
                                                        torrent_info=context.torrent_info,
                                                        episodes=episodes,
                                                        downloader=downloader)

            self.__with_lock_and_update_torrent_tasks(
                method=lambda tasks: tasks.update({
                    torrent_hash: {
                        "hash": torrent_hash,
                        "subscribe_id": subscribe.id if subscribe else None,
                        "subscribe_info": subscribe_info,
                        "episodes": episodes,
                        "username": username,
                        "downloader": downloader,
                        "site_id": context.torrent_info.site,
                        "site_name": context.torrent_info.site_name,
                        "title": context.torrent_info.title,
                        "description": context.torrent_info.description,
                        "enclosure": context.torrent_info.enclosure,
                        "page_url": context.torrent_info.page_url,
                        "pending_check": self._auto_download_pending,
                        "timeout_check": self._auto_download_delete,
                        "manual_check": self._manual_delete_listen,
                        "time": time.time(),
                    }
                })
            )
        except Exception as e:
            logger.error(f"处理下载添加事件时发生错误: {str(e)}", exc_info=True)

    @eventmanager.register(ChainEventType.ResourceSelection)
    def handle_resource_selection_event(self, event: Event):
        """
        处理资源选择事件
        """
        if not event or not event.event_data:
            return

        event_data: ResourceSelectionEventData = event.event_data
        if not event_data.contexts:
            return

        logger.debug(f"接收到资源选择事件，资源数: {len(event_data.contexts)}")

        subscribe_info, subscribe = self.__get_subscribe_by_source(source=event_data.origin)
        if not subscribe_info or not subscribe:
            logger.debug(f"未能找到订阅信息，跳过处理")
            return

        # 检查是否开启下载自动待定，并且当前是否处于待定状态
        if self._auto_download_pending:
            subscribe_tasks = self.__get_data(key="subscribes")
            subscribe_task, exists = self.__initialize_subscribe_task(subscribe=subscribe,
                                                                      subscribe_tasks=subscribe_tasks)

            # 如果存在洗版订阅任务，并处于下载待定状态，则不允许进行资源选择，否则可能会多次下载资源
            if subscribe.best_version and subscribe.state == "P" and exists:
                pending = self.__get_subscribe_task_download_pending(subscribe_task=subscribe_task)
                if pending:
                    logger.info(f"{self.__format_subscribe(subscribe=subscribe)} 当前存在任务正在下载，取消后续资源选择")
                    event_data.updated = True
                    event_data.updated_contexts = []
                    event_data.source = self.plugin_name
                    return

        # 跳过删除记录未开启
        if not self._skip_deletion:
            logger.debug("跳过删除记录功能未开启，跳过处理")
            return

        delete_tasks = self.__get_data("deletes")
        if not delete_tasks:
            return

        # 处理超时删除任务
        updated = False
        update_contexts = event_data.updated_contexts or event_data.contexts or []
        for context in list(update_contexts):
            torrent_info = context.torrent_info
            if not torrent_info:
                continue
            for torrent_task in delete_tasks.values():
                if self.__compare_torrent_info_and_task(torrent_info=torrent_info, torrent_task=torrent_task,
                                                        partial_match=True):
                    logger.info(f"存在超时/手动删除的种子信息，跳过，context：{context}")
                    update_contexts.remove(context)
                    updated = True
                    continue
        if updated:
            event_data.updated = True
            event_data.updated_contexts = update_contexts
            event_data.source = self.plugin_name

    @eventmanager.register(etype=ChainEventType.ResourceDownload, priority=9999)
    def handle_resource_download_event(self, event: Event):
        """
        处理资源下载事件
        """
        if not event or not event.event_data:
            return

        logger.debug(f"接收到资源下载事件，资源信息: {event.event_data}")

        event_data: ResourceDownloadEventData = event.event_data
        if event_data.cancel:
            logger.debug(f"该事件已被其他事件处理器处理，跳过后续操作")
            return

        # 获取种子信息
        context: Context = event_data.context
        downloader = event_data.downloader
        episodes = list(event_data.episodes or [])
        if not context or not context.torrent_info:
            logger.info("没有获取到有效的种子任务信息，跳过处理")
            return

        # 查找订阅信息
        subscribe_info, subscribe = self.__get_subscribe_by_source(source=event_data.origin)
        if not subscribe_info or not subscribe:
            logger.debug(f"未能找到订阅信息，跳过处理")
            return

        self.__handle_resource_download_pending(subscribe=subscribe, context=context,
                                                episodes=episodes, downloader=downloader)

        self.__handle_resource_download_history_clear(subscribe=subscribe)

    @eventmanager.register(etype=ChainEventType.TransferIntercept, priority=9999)
    def handle_transfer_intercept_event(self, event: Event):
        """
        处理整理拦截事件
        """
        if not event or not event.event_data:
            return

        logger.debug(f"接收到整理拦截事件，事件信息: {event.event_data}")

        event_data: TransferInterceptEventData = event.event_data
        if event_data.cancel:
            logger.debug(f"该事件已被其他事件处理器处理，跳过后续操作")
            return

        if not event_data.mediainfo:
            logger.debug(f"未能获取到媒体信息，跳过后续操作")
            return

        self.__handle_transfer_intercept_history_clear(mediainfo=event_data.mediainfo,
                                                       target_path=event_data.target_path)

    @eventmanager.register(etype=EventType.TransferComplete)
    def handle_transfer_complete_event(self, event: Event):
        """
        处理整理完成事件
        """
        if not event or not event.event_data:
            return

        event_data = event.event_data
        transfer_info: TransferInfo = event_data.get("transferinfo")
        downloader = event_data.get("downloader")
        download_hash = event_data.get("download_hash")
        if not transfer_info:
            logger.debug(f"未能获取到整理信息，跳过后续操作")
            return

        logger.debug(
            f"接收到整理完成事件，整理文件信息: {transfer_info.fileitem}，整理类型：{transfer_info.transfer_type}，"
            f"下载器：{downloader}，种子：{download_hash}")

        self.__handle_transfer_complete_remove_torrent(transfer_info, downloader, download_hash)

    def __handle_resource_download_pending(self, subscribe: Subscribe, context: Context,
                                           episodes: list, downloader: str):
        """
        处理资源下载自动待定
        """
        # 下载自动待定功能未开启
        if not self._auto_download_pending:
            logger.debug("下载自动待定功能未开启，跳过处理")
            return

        # 更新订阅下载任务
        self.__with_lock_and_update_subscribe_tasks(method=self.__update_subscribe_torrent_task,
                                                    subscribe=subscribe,
                                                    torrent_info=context.torrent_info,
                                                    episodes=episodes,
                                                    downloader=downloader,
                                                    pending=True,
                                                    update_priority=True)

        # 更新订阅信息为待定
        logger.debug(f"{self.__format_subscribe(subscribe)} 已更新为待定状态")
        if subscribe.state != "P":
            self.subscribe_oper.update(subscribe.id, {"state": "P"})

        logger.debug(f"已完成资源下载自动待定处理")

    def __handle_resource_download_history_clear(self, subscribe: Subscribe):
        """
        处理洗版资源下载时清理整理记录
        """
        if not subscribe.best_version:
            return

        # 如果订阅类型不在清理整理记录的策略中，则直接返回
        subscribe_type = MediaType(subscribe.type)
        if subscribe_type not in self._auto_best_clear_history_types:
            logger.debug(f"{self.__format_subscribe(subscribe)}，尚未开启清理整理记录，跳过处理")
            return

        if not subscribe.tmdbid:
            logger.warning(f"{self.__format_subscribe(subscribe)} 未能获取到 TMDBID，跳过处理")

        if subscribe_type == MediaType.TV:
            meta = self.__get_subscribe_meta(subscribe)
            histories = self.transferhistory_oper.get_by(tmdbid=subscribe.tmdbid, mtype=subscribe.type,
                                                         season=meta.season)
        else:
            histories = self.transferhistory_oper.get_by(tmdbid=subscribe.tmdbid, mtype=subscribe.type)

        if not histories:
            logger.info(
                f"{self.__format_subscribe(subscribe)} TMDBID: {subscribe.tmdbid} 未能获取到匹配的整理记录，跳过处理")
            return

        logger.info(f"即将开始清理洗版资源整理源文件记录")

        logger.info(
            f"{self.__format_subscribe(subscribe)} TMDBID: {subscribe.tmdbid} 获取到 {len(histories)} 条整理记录，即将开始清理")

        with lock:
            tasks = self.__get_data(key="best_version_clear_histories")

            self.__clear_transfer_src_histories(tasks=tasks, subscribe=subscribe, histories=histories)

            self.__save_data(key="best_version_clear_histories", value=tasks)

        # 强制睡眠5s，等待所有外部事件处理完成，如下载器种子清理等等
        time.sleep(5)

    def __clear_transfer_src_histories(self, tasks: dict, subscribe: Subscribe,
                                       histories: list[TransferHistory]):
        """
        清理整理源文件历史记录
        :param tasks: 任务集合
        :param histories: 整理历史记录
        """
        if not subscribe.tmdbid or not histories:
            return

        tasks[subscribe.tmdbid] = {
            "subscribe_id": subscribe.id,
            "subscribe_desc": self.__format_subscribe_desc(subscribe=subscribe),
            "subscribe_image": self.__get_subscribe_image(subscribe=subscribe),
            "histories": [history.to_dict() for history in histories],
            "time": time.time(),
        }

        storge_chain = StorageChain()
        for history in histories:
            logger.info(f"清理整理记录并删除源文件：{history.src}")

            # 删除源文件
            if history.src_fileitem:
                src_fileitem = schemas.FileItem(**history.src_fileitem)
                state = storge_chain.delete_media_file(src_fileitem)
                if not state:
                    logger.warning(f"{src_fileitem.path} 删除失败")
                # 发送事件
                eventmanager.send_event(
                    EventType.DownloadFileDeleted,
                    {
                        "src": history.src,
                        "hash": history.download_hash
                    }
                )
            # 删除记录
            self.transferhistory_oper.delete(history.id)

        if self._notify:
            self.post_message(
                mtype=NotificationType.Subscribe,
                title=f"{self.__format_subscribe_desc(subscribe=subscribe)} 即将开始洗版下载",
                text=f"已删除 {len(histories)} 条整理记录对应的源文件",
                image=self.__get_subscribe_image(subscribe),
            )

    def __handle_transfer_intercept_history_clear(self, mediainfo: MediaInfo, target_path: Path):
        """
        处理洗版资源整理时清理整理记录
        """
        if not mediainfo:
            return

        if not mediainfo.tmdb_id:
            logger.warning(f"{self.__format_subscribe_desc(mediainfo=mediainfo)} 未能获取到 TMDBID，跳过处理")

        with lock:
            tasks = self.__get_data(key="best_version_clear_histories")
            clear_key = str(mediainfo.tmdb_id)
            task = tasks.get(clear_key)
            if not task:
                return

            if self.__clear_transfer_dest_histories(task=task, mediainfo=mediainfo, target_path=target_path):
                del tasks[clear_key]
                self.__save_data(key="best_version_clear_histories", value=tasks)

    def __clear_transfer_dest_histories(self, task: dict, mediainfo: MediaInfo, target_path: Path) -> bool:
        """
        清理整理媒体库文件历史记录
        :param task: 任务
        :param mediainfo: 媒体信息
        :param target_path: 目标路径
        """
        if not task or not mediainfo:
            return False

        histories = task.get("histories")
        if not histories:
            return True

        logger.info(f"即将开始清理洗版资源整理媒体库文件记录")

        subscribe_desc = task.get("subscribe_desc")
        subscribe_image = task.get("subscribe_image")

        logger.info(f"{subscribe_desc} TMDBID: {mediainfo.tmdb_id} 获取到 {len(histories)} 条整理记录，即将开始清理")

        storge_chain = StorageChain()
        for history in histories:
            dest = history.get("dest")
            dest_fileitem = history.get("dest_fileitem")
            logger.info(f"清理整理记录并删除媒体库文件：{dest}")

            # 删除媒体库文件
            if dest_fileitem:
                dest_fileitem = schemas.FileItem(**dest_fileitem)
                state = storge_chain.delete_media_file(dest_fileitem)
                if not state:
                    logger.warning(f"{dest_fileitem.path} 删除失败")

        if self._notify:
            self.post_message(
                mtype=NotificationType.Subscribe,
                title=f"{subscribe_desc} 即将开始洗版整理",
                text=f"已删除 {len(histories)} 条整理记录对应的媒体库文件",
                image=subscribe_image,
            )
        return True

    def __handle_transfer_complete_remove_torrent(self, transfer_info: TransferInfo, downloader: str,
                                                  download_hash: str):
        """
        处理整理完成移动模式种子删除同步
        """
        if not transfer_info or transfer_info.transfer_type != "move":
            return

        if not downloader or not download_hash:
            return

        with lock:
            # 获取订阅任务和种子任务数据
            subscribe_tasks = self.__get_data(key="subscribes")
            torrent_tasks = self.__get_data(key="torrents")
            if download_hash not in torrent_tasks:
                return
            torrent_task = torrent_tasks[download_hash]
            torrent_desc = self.__get_torrent_desc(torrent_hash=download_hash, torrent_task=torrent_task)
            self.__clean_invalid_torrents(invalid_torrent_hashes=[download_hash], subscribe_tasks=subscribe_tasks,
                                          torrent_tasks=torrent_tasks)
            logger.info(f"订阅种子 {torrent_desc} 已整理入库，整理类型：{transfer_info.transfer_type}，相关订阅任务已清理")
            # 保存更新后的数据
            self.__save_data(key="subscribes", value=subscribe_tasks)
            self.__save_data(key="torrents", value=torrent_tasks)

    def __get_downloader_service(self, downloader: str) -> Optional[ServiceInfo]:
        """
        获取下载器服务
        """
        service = self.downloader_helper.get_service(name=downloader)
        if not service:
            logger.error(f"{downloader} 获取下载器实例失败，请检查配置")
            return None

        return service

    @staticmethod
    def __get_torrents(downloader: Optional[Union[Qbittorrent, Transmission]],
                       torrent_hashes: Optional[Union[str, List[str]]] = None) -> Optional[Any]:
        """
        获取下载器中的种子信息
        :param downloader: 下载器实例
        :param torrent_hashes: 单个种子哈希或包含多个种子 hash 的列表
        :return: 单个种子的具体信息或包含多个种子信息的列表
        """
        if not downloader:
            logger.warning(f"获取下载器实例失败，请稍后重试")
            return None

        # 处理单个种子哈希的情况，确保其被视为列表
        if isinstance(torrent_hashes, str):
            torrent_hashes = [torrent_hashes]

        torrents, error = downloader.get_torrents(ids=torrent_hashes)
        if error:
            logger.warning(f"连接下载器出错，请稍后重试")
            return None

        # 如果只有一个种子哈希，直接返回该种子的信息
        if torrent_hashes and len(torrent_hashes) == 1:
            return torrents[0] if torrents else None

        return torrents

    @staticmethod
    def __delete_torrents(downloader: Optional[Union[Qbittorrent, Transmission]],
                          torrent_hashes: Optional[Union[str, List[str]]] = None) -> bool:
        """
        删除下载器中的种子
        :param downloader: 下载器实例
        :param torrent_hashes: 单个种子哈希或包含多个种子 hash 的列表
        :return: 单个种子的具体信息或包含多个种子信息的列表
        """
        if not downloader:
            logger.warning(f"获取下载器实例失败，请稍后重试")
            return False

        # 处理单个种子哈希的情况，确保其被视为列表
        if isinstance(torrent_hashes, str):
            torrent_hashes = [torrent_hashes]

        deleted = downloader.delete_torrents(delete_file=True, ids=torrent_hashes)
        if not deleted:
            logger.warning(f"删除种子过程中发生异常，请检查")
            return False

        return deleted

    @staticmethod
    def __get_torrent_tags(torrent: Any, dl_type: str) -> list[str]:
        """
        获取种子标签
        """
        try:
            if dl_type == "qbittorrent":
                tags = torrent.get("tags", "").split(",")
            else:
                tags = torrent.labels or []

            return list(set(tag.strip() for tag in tags if tag.strip()))
        except Exception as e:
            logger.error(f"获取种子标签失败，错误: {e}", exc_info=True)
            return []

    @staticmethod
    def __get_torrent_info(torrent: Any, dl_type: str) -> dict:
        """
        获取种子信息
        """
        date_now = int(time.time())
        # QB
        if dl_type == "qbittorrent":
            """
            {
              "added_on": 1693359031,
              "amount_left": 0,
              "auto_tmm": false,
              "availability": -1,
              "category": "tJU",
              "completed": 67759229411,
              "completion_on": 1693609350,
              "content_path": "/mnt/sdb/qb/downloads/Steel.Division.2.Men.of.Steel-RUNE",
              "dl_limit": -1,
              "dlspeed": 0,
              "download_path": "",
              "downloaded": 67767365851,
              "downloaded_session": 0,
              "eta": 8640000,
              "f_l_piece_prio": false,
              "force_start": false,
              "hash": "116bc6f3efa6f3b21a06ce8f1cc71875",
              "infohash_v1": "116bc6f306c40e072bde8f1cc71875",
              "infohash_v2": "",
              "last_activity": 1693609350,
              "magnet_uri": "magnet:?xt=",
              "max_ratio": -1,
              "max_seeding_time": -1,
              "name": "Steel.Division.2.Men.of.Steel-RUNE",
              "num_complete": 1,
              "num_incomplete": 0,
              "num_leechs": 0,
              "num_seeds": 0,
              "priority": 0,
              "progress": 1,
              "ratio": 0,
              "ratio_limit": -2,
              "save_path": "/mnt/sdb/qb/downloads",
              "seeding_time": 615035,
              "seeding_time_limit": -2,
              "seen_complete": 1693609350,
              "seq_dl": false,
              "size": 67759229411,
              "state": "stalledUP",
              "super_seeding": false,
              "tags": "",
              "time_active": 865354,
              "total_size": 67759229411,
              "tracker": "https://tracker",
              "trackers_count": 2,
              "up_limit": -1,
              "uploaded": 0,
              "uploaded_session": 0,
              "upspeed": 0
            }
            """
            # ID
            torrent_id = torrent.get("hash")
            # 标题
            torrent_title = torrent.get("name")
            # 下载时间
            if (not torrent.get("added_on")
                    or torrent.get("added_on") < 0):
                dltime = 0
            else:
                dltime = date_now - torrent.get("added_on")
            # 做种时间
            if (not torrent.get("completion_on")
                    or torrent.get("completion_on") < 0):
                seeding_time = 0
            else:
                seeding_time = date_now - torrent.get("completion_on")
            # 分享率
            ratio = torrent.get("ratio") or 0
            # 上传量
            uploaded = torrent.get("uploaded") or 0
            # 平均上传速度 Byte/s
            if dltime:
                avg_upspeed = int(uploaded / dltime)
            else:
                avg_upspeed = uploaded
            # 已未活动 秒
            if (not torrent.get("last_activity")
                    or torrent.get("last_activity") < 0):
                iatime = 0
            else:
                iatime = date_now - torrent.get("last_activity")
            # 下载量
            downloaded = torrent.get("downloaded")
            # 种子大小
            total_size = torrent.get("total_size")
            # 目标大小
            target_size = torrent.get("size")
            # 添加时间
            add_on = (torrent.get("added_on") or 0)
            add_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(add_on))
            # 种子标签
            tags = torrent.get("tags")
            # tracker
            tracker = torrent.get("tracker")
            # tracker_responses
            trackers = [tracker for tracker in torrent.trackers if tracker.tier != -1] if torrent.trackers else []
            tracker_responses = [tracker.msg for tracker in trackers if tracker.msg] if trackers else []
            # state
            state = torrent.get("state")
        # TR
        else:
            # ID
            torrent_id = torrent.hashString
            # 标题
            torrent_title = torrent.name
            # 做种时间
            if (not torrent.date_done
                    or torrent.date_done.timestamp() < 1):
                seeding_time = 0
            else:
                seeding_time = date_now - int(torrent.date_done.timestamp())
            # 下载耗时
            if (not torrent.date_added
                    or torrent.date_added.timestamp() < 1):
                dltime = 0
            else:
                dltime = date_now - int(torrent.date_added.timestamp())
            # 下载量
            downloaded = int(torrent.total_size * torrent.progress / 100)
            # 分享率
            ratio = torrent.ratio or 0
            # 上传量
            uploaded = int(downloaded * torrent.ratio)
            # 平均上传速度
            if dltime:
                avg_upspeed = int(uploaded / dltime)
            else:
                avg_upspeed = uploaded
            # 未活动时间
            if (not torrent.date_active
                    or torrent.date_active.timestamp() < 1):
                iatime = 0
            else:
                iatime = date_now - int(torrent.date_active.timestamp())
            # 种子大小
            total_size = torrent.total_size
            # 目标大小
            target_size = torrent.size_when_done if "size_when_done" in torrent.fields else total_size
            # 添加时间
            add_on = (torrent.date_added.timestamp() if torrent.date_added else 0)
            add_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(add_on))
            # 种子标签
            tags = torrent.get("tags")
            # tracker
            tracker = torrent.get("tracker")
            # tracker_responses
            trackers = [tracker for tracker in torrent.tracker_stats if
                        tracker.tier != -1] if torrent.tracker_stats else []
            tracker_responses = [tracker.last_announce_result for tracker in trackers if
                                 tracker.last_announce_result] if trackers else []
            # state
            state = torrent.status
        return {
            "hash": torrent_id,
            "title": torrent_title,
            "seeding_time": seeding_time,
            "ratio": ratio,
            "uploaded": uploaded,
            "downloaded": downloaded,
            "avg_upspeed": avg_upspeed,
            "iatime": iatime,
            "dltime": dltime,
            "total_size": total_size,
            "target_size": target_size,
            "add_time": add_time,
            "add_on": add_on,
            "tags": tags,
            "tracker": tracker,
            "tracker_responses": tracker_responses,
            "state": state,
        }

    @staticmethod
    def __get_torrent_completion_status(torrent_info: dict) -> Tuple[bool, float]:
        """
        获取种子的完成状态和相关时间信息
        :param torrent_info: 包含种子信息的字典，必须包含种子状态、下载大小、总大小等字段
        :return: 返回一个元组，第一个元素是布尔值，表示种子是否完成，第二个元素是完成的时间（如果已完成，返回0；否则返回下载时间）
        """
        if not torrent_info:
            return False, -1

        # 如果种子正在做种，说明已完成
        torrent_state = torrent_info.get("state")
        if torrent_state in ["seeding", "seed_pending"]:
            return True, 0

        # 如果存在做种时间，说明已完成
        if torrent_info.get("seeding_time"):
            return True, 0

        # 如果种子的已下载大小大于目标大小，说明已完成
        if torrent_info.get("downloaded") >= torrent_info.get("target_size"):
            return True, 0

        return False, torrent_info.get("dltime")

    def __get_subscribe_by_source(self, source: str) -> Tuple[Optional[dict], Optional[Subscribe]]:
        """
        从来源获取订阅信息
        """
        if not source or "|" not in source:
            logger.debug("未找到有效的订阅来源信息，跳过处理")
            return None, None

        prefix, json_data = source.split("|", 1)
        if prefix != "Subscribe":
            logger.debug(f"source 前缀不符合订阅预期值: {prefix}，跳过处理")
            return None, None

        try:
            subscribe_dict = json.loads(json_data)
        except Exception as e:
            logger.error(f"解析 source 数据失败，source: {json_data}, 错误: {e}", exc_info=True)
            return None, None

        subscribe_id = subscribe_dict.get("id")
        subscribe = self.subscribe_oper.get(subscribe_id)
        return subscribe_dict, subscribe

    def process_delete_task(self):
        """
        清理超时种子记录
        """
        self.__with_lock_and_update_delete_tasks(method=self.__process_delete_task)

    def __process_delete_task(self, torrent_tasks: dict):
        """
        清理超时种子记录
        :param torrent_tasks: 种子任务字典
        """
        if not torrent_tasks:
            return

        if not self._timeout_history_cleanup:
            logger.debug("未配置超时记录清理时间，跳过处理")
            return

        if self._timeout_history_cleanup <= 0:
            logger.debug("超时记录清理时间小于等于0，跳过处理")
            return

        current_time = time.time()
        timeout_threshold = self._timeout_history_cleanup * 3600

        # 遍历torrent_tasks字典，移除超时的记录
        for torrent_hash in list(torrent_tasks.keys()):
            torrent_task = torrent_tasks[torrent_hash]
            delete_time = torrent_task.get("delete_time")
            if not delete_time:
                del torrent_tasks[torrent_hash]
                continue
            elapsed_time = current_time - delete_time
            if elapsed_time > timeout_threshold:
                logger.info(f"超时种子记录 {torrent_hash} 已满足清理时间，删除任务")
                del torrent_tasks[torrent_hash]

    def process_download_task(self):
        """
        处理下载种子任务并清理异常种子
        """
        if not (self._auto_download_delete or self._manual_delete_listen or
                self._tracker_response_listen or self._auto_download_pending):
            return

        with lock:
            # 获取订阅任务和种子任务数据
            subscribe_tasks = self.__get_data(key="subscribes")
            torrent_tasks = self.__get_data(key="torrents")
            # 处理下载种子任务
            self.__process_download_task(subscribe_tasks=subscribe_tasks, torrent_tasks=torrent_tasks)
            # 重置订阅待定状态
            self.__reset_subscribe_task_pending(subscribe_tasks=subscribe_tasks)
            # 保存更新后的数据
            self.__save_data(key="subscribes", value=subscribe_tasks)
            self.__save_data(key="torrents", value=torrent_tasks)

    def __process_download_task(self, subscribe_tasks: dict, torrent_tasks: dict):
        """
        处理下载种子任务并清理异常种子
        :param subscribe_tasks: 订阅任务字典
        :param torrent_tasks: 下载任务字典
        """
        # 用于存储异常的种子
        invalid_torrent_hashes = []
        triggered_subscribe_ids = set()
        for torrent_hash, torrent_task in list(torrent_tasks.items()):
            subscribe_id = torrent_task.get("subscribe_id")
            subscribe_info = torrent_task.get("subscribe_info")
            username = torrent_task.get("username")
            downloader = torrent_task.get("downloader")
            site_id = torrent_task.get("site_id")
            site_name = torrent_task.get("site_name")
            title = torrent_task.get("title")
            description = torrent_task.get("description")
            enclosure = torrent_task.get("enclosure")
            page_url = torrent_task.get("page_url")
            pending_check = torrent_task.get("pending_check")
            timeout_check = torrent_task.get("timeout_check")
            manual_check = torrent_task.get("manual_check")
            torrent_time = torrent_task.get("time")
            torrent_desc = self.__get_torrent_desc(torrent_hash=torrent_hash, torrent_task=torrent_task)

            subscribe_task = subscribe_tasks.get(str(subscribe_id))
            if not subscribe_task:
                logger.debug(f"未找到相关的订阅信息，种子任务: {torrent_desc}")
                invalid_torrent_hashes.append(torrent_hash)
                continue

            subscribe = self.subscribe_oper.get(subscribe_id)
            if not subscribe:
                logger.debug(f"数据库中未找到相关的订阅信息，种子任务: {torrent_desc}")
                invalid_torrent_hashes.append(torrent_hash)
                continue

            if not self.__match_subscribe(subscribe=subscribe, subscribe_task=subscribe_task):
                logger.debug(f"关联的订阅信息与当前订阅信息不匹配，种子任务: {torrent_desc}")
                invalid_torrent_hashes.append(torrent_hash)
                continue

            if not self.__check_subscribe_status(subscribe=subscribe):
                continue

            subscribe_torrent_tasks = subscribe_task.get("torrent_tasks") or []
            subscribe_torrent_task = {}
            for task in subscribe_torrent_tasks:
                if task.get("hash") == torrent_hash:
                    subscribe_torrent_task = task
                    break

            if not subscribe_torrent_task:
                logger.debug(f"未找到对应的订阅种子任务，种子任务: {torrent_desc}")
                invalid_torrent_hashes.append(torrent_hash)
                continue

            service = self.__get_downloader_service(downloader=downloader)
            if not service:
                logger.debug(f"获取下载器 {downloader} 实例失败，请检查配置，种子任务: {torrent_desc}")
                invalid_torrent_hashes.append(torrent_hash)
                continue

            if service.instance.is_inactive():
                logger.debug(f"下载器 {service.name} 未连接")
                # 部分情况下，下载器可能会失联，这里不在直接移除种子
                continue

            torrent = self.__get_torrents(downloader=service.instance, torrent_hashes=torrent_hash)
            if not torrent:
                logger.info(f"没有获取到对应的种子详情，种子可能已被删除，种子任务: {torrent_desc}")
                if not manual_check or not self._manual_delete_listen:
                    invalid_torrent_hashes.append(torrent_hash)
                    continue
                else:
                    self.__clean_torrent_task_by_hash(subscribe=subscribe, subscribe_task=subscribe_task,
                                                      subscribe_torrent_tasks=subscribe_torrent_tasks,
                                                      triggered_subscribe_ids=triggered_subscribe_ids,
                                                      torrent_hash=torrent_hash, torrent_task=torrent_task,
                                                      torrent_tasks=torrent_tasks, reason="订阅种子手动删除")
                    continue

            torrent_info = self.__get_torrent_info(torrent=torrent, dl_type=service.type)
            if not torrent_info:
                invalid_torrent_hashes.append(torrent_hash)
                logger.info(f"没有获取到对应的种子详情，可能是不支持的种子类型，种子任务: {torrent_desc}")
                continue

            is_completed, download_time = self.__get_torrent_completion_status(torrent_info=torrent_info)
            if is_completed:
                logger.info(f"种子 {torrent_desc} 已完成，将从订阅种子任务中移除")

                if torrent_hash in torrent_tasks:
                    del torrent_tasks[torrent_hash]

                subscribe_task["torrent_tasks"] = [
                    task for task in subscribe_torrent_tasks if task.get("hash") != torrent_hash
                ]
            else:
                logger.debug(f"种子任务 {torrent_desc} 尚未完成，下载时长 {download_time / 3600 :.2f}")

                deletion_reason = None
                # 1. 判断 Tracker 响应关键字是否满足删除条件
                if self._tracker_response_listen and self._tracker_responses:
                    tracker_responses = torrent_info.get("tracker_responses") or []
                    if tracker_responses:
                        matched_keyword = None
                        for pattern in self._tracker_responses:
                            for response in tracker_responses:
                                # 使用正则匹配（可在 pattern 内部或通过 re.I 忽略大小写）
                                if re.search(pattern, response, re.I):
                                    matched_keyword = pattern
                                    break
                            if matched_keyword:
                                break
                        if matched_keyword:
                            deletion_reason = f"订阅种子，命中 Tracker 响应关键字（{matched_keyword}）"

                # 2. 判断超时删除条件（只有当 Tracker 未触发时才检查超时）
                if not deletion_reason:
                    if timeout_check and self._auto_download_delete and download_time >= self._download_timeout * 3600:
                        deletion_reason = f"订阅种子，下载时长超时（{download_time / 3600 :.2f}）"

                if not deletion_reason:
                    continue

                # 3. 如果满足删除条件，则统一调用删除接口，检查是否存在排除删除的标签
                if self._delete_exclude_tags:
                    torrent_tags = self.__get_torrent_tags(torrent=torrent, dl_type=service.type)
                    if torrent_tags:
                        # 对配置的排除标签进行 trim 并转换为集合
                        exclude_tags = set(
                            tag.strip() for tag in self._delete_exclude_tags.split(",") if tag.strip())
                        intersection_tags = exclude_tags & set(torrent_tags)
                        if intersection_tags:
                            logger.debug(f"种子任务 {torrent_desc} 满足删除条件（{deletion_reason}），"
                                         f"但满足不删除标签 {intersection_tags}，跳过处理")
                            continue

                logger.info(f"种子任务 {torrent_desc} 满足删除条件：{deletion_reason}，即将删除并从订阅种子任务中移除")
                self.__delete_torrents(downloader=service.instance, torrent_hashes=torrent_hash)
                self.__clean_torrent_task_by_hash(
                    subscribe=subscribe,
                    subscribe_task=subscribe_task,
                    subscribe_torrent_tasks=subscribe_torrent_tasks,
                    triggered_subscribe_ids=triggered_subscribe_ids,
                    torrent_hash=torrent_hash,
                    torrent_task=torrent_task,
                    torrent_tasks=torrent_tasks,
                    reason=deletion_reason
                )

        self.__clean_invalid_torrents(invalid_torrent_hashes, subscribe_tasks, torrent_tasks)

    def __clean_torrent_task_by_hash(self, subscribe: Subscribe, subscribe_task: dict,
                                     subscribe_torrent_tasks: list[dict], triggered_subscribe_ids: set,
                                     torrent_hash: str, torrent_task: dict, torrent_tasks: dict,
                                     reason: str):
        """
          清理并更新种子下载记录

          :param subscribe: 当前订阅对象
          :param subscribe_task: 当前订阅的任务记录
          :param subscribe_torrent_tasks: 订阅下所有种子的任务记录
          :param triggered_subscribe_ids: 被触发的订阅 ID 集合
          :param torrent_hash: 种子哈希值
          :param torrent_task: 当前种子任务信息
          :param torrent_tasks: 所有种子任务的字典
          """
        if torrent_hash in torrent_tasks:
            del torrent_tasks[torrent_hash]

        subscribe_task["torrent_tasks"] = [
            task for task in subscribe_torrent_tasks if task.get("hash") != torrent_hash
        ]

        # 记录删除记录
        self.__with_lock_and_update_delete_tasks(method=self.__update_or_add_delete_tasks,
                                                 torrent_task=torrent_task)

        # 处理删除后续逻辑
        self.__handle_timeout_seed_deletion(subscribe=subscribe, subscribe_task=subscribe_task,
                                            torrent_task=torrent_task,
                                            triggered_subscribe_ids=triggered_subscribe_ids, reason=reason)

    def __reset_subscribe_task_pending(self, subscribe_tasks: dict):
        """
       重置订阅待定状态

       :param subscribe_tasks: 订阅任务
       """
        if not subscribe_tasks:
            return
        for subscribe_id, subscribe_task in subscribe_tasks.items():
            subscribe = self.subscribe_oper.get(sid=subscribe_id)
            if not self.__check_subscribe_status(subscribe=subscribe):
                continue
            pending = self.__get_subscribe_task_pending(subscribe_task=subscribe_task)
            # 如果当前订阅状态为待定，且订阅任务不为待定状态，则更新为订阅中
            if subscribe.state == "P" and not pending:
                self.subscribe_oper.update(subscribe.id, {"state": "R"})
                logger.info(f"{self.__format_subscribe(subscribe)} 状态从 {subscribe.state} 更新为 R")

    def __handle_timeout_seed_deletion(self, subscribe: Subscribe, subscribe_task: dict, torrent_task: dict,
                                       triggered_subscribe_ids: set, reason: str):
        """
        处理删除超时种子后续相关任务

        :param subscribe: 订阅信息
        :param subscribe_task: 订阅任务
        :param torrent_task: 种子任务
        :param triggered_subscribe_ids: 已触发的订阅任务
        :param reason: 原因
        """
        if not subscribe:
            return

        media_type = MediaType(subscribe.type)
        update_data = {}
        if media_type == MediaType.TV:
            episodes = torrent_task.get("episodes") or []
            note = set(subscribe.note or [])
            episodes_set = set(episodes)
            note = list(note - episodes_set)
            update_data["note"] = note
            if subscribe.total_episode:
                start_episode = subscribe.start_episode - 1 if subscribe.start_episode else 0
                lack_episode = subscribe.total_episode - start_episode - len(note)
                update_data["lack_episode"] = lack_episode
            else:
                update_data["lack_episode"] = subscribe.total_episode
        elif media_type == MediaType.MOVIE:
            update_data["note"] = []

        # 如果是洗版，这里还需要处理优先级
        if subscribe.best_version:
            update_data["current_priority"] = subscribe_task.get("current_priority", subscribe.current_priority) or 0
        if update_data:
            self.subscribe_oper.update(subscribe.id, update_data)

        random_minutes = random.uniform(3, 5)
        completion_time = f"{random_minutes:.2f} 分钟"

        if self._notify:
            # 构建消息内容
            msg_parts = []
            if torrent_task.get("title"):
                msg_parts.append(f"标题：{torrent_task.get('title')}")
            if torrent_task.get("description"):
                msg_parts.append(f"内容：{torrent_task.get('description')}")
            if self._auto_search_when_delete:
                msg_parts.append(f"补全：将在 {completion_time} 后触发搜索")
            # 拼接消息文本
            msg_text = "\n".join(msg_parts)
            # 推送消息
            self.post_message(
                mtype=NotificationType.Subscribe,
                title=f"{self.__format_subscribe_desc(subscribe=subscribe)} {reason}，已删除",
                text=msg_text,
                image=self.__get_subscribe_image(subscribe),
            )

        if not self._auto_search_when_delete:
            return

        # 如果这个订阅已经触发过补全搜索任务，直接返回
        if subscribe.id in triggered_subscribe_ids:
            return
        triggered_subscribe_ids.add(subscribe.id)
        logger.info(f"{self.__format_subscribe(subscribe)}，{reason}，触发补全搜索任务，"
                    f"任务将在 {random_minutes:.2f} 分钟后触发")
        timer = threading.Timer(random_minutes * 60,
                                lambda sid=subscribe.id: SubscribeChain().search(sid=sid))
        timer.start()

    def __clean_invalid_torrents(self, invalid_torrent_hashes: list, subscribe_tasks: dict, torrent_tasks: dict):
        """
        清理异常种子

        :param invalid_torrent_hashes: 异常种子哈希列表
        :param subscribe_tasks: 所有订阅任务
        :param torrent_tasks: 所有下载任务
        """
        # 从 subscribe_tasks 中移除与异常种子相关的订阅任务
        for torrent_hash in invalid_torrent_hashes:
            # 从 torrent_tasks 中移除异常种子
            torrent_task = torrent_tasks.get(torrent_hash)
            if not torrent_task:
                continue
            torrent_desc = self.__get_torrent_desc(torrent_hash, torrent_task)
            logger.info(f"清理异常种子：{torrent_desc}")
            del torrent_tasks[torrent_hash]

            # 从订阅任务中移除异常种子
            for subscribe_task in subscribe_tasks.values():
                subscribe_task["torrent_tasks"] = [
                    task for task in subscribe_task.get("torrent_tasks", []) if task.get("hash") != torrent_hash
                ]

    @staticmethod
    def __get_torrent_desc(torrent_hash: str, torrent_task: dict) -> str:
        """
        获取种子的描述信息

        :param torrent_hash: 种子hash
        :param torrent_task: 种子任务

        :return: 种子的描述字符串
        """
        title = torrent_task.get("title")
        description = torrent_task.get("description")
        desc_part = f"| {description} " if description else ""
        return f"{title}{desc_part}({torrent_hash})"

    def __reset_subscribe_task_state_when_updated(self, subscribe_tasks: dict, subscribe: Subscribe,
                                                  different_keys: dict):
        """
        重新订阅状态
        :param subscribe_tasks: 订阅任务字典
        :param subscribe: 订阅对象
        :param different_keys: 变更字段
        """
        if not subscribe or subscribe_tasks is None:
            return

        logger.debug(f"{self.__format_subscribe(subscribe=subscribe)} 已更新，变更字段：{different_keys}")

        if "state" not in different_keys:
            logger.debug(f"{self.__format_subscribe(subscribe=subscribe)} 状态未更新，跳过处理")
            return

        # 获取或初始化订阅任务
        subscribe_task, exists = self.__initialize_subscribe_task(subscribe, subscribe_tasks)
        subscribe_task["pause_for_user"] = False
        subscribe_task["pause_for_user_time"] = None
        subscribe_task["pause_for_download"] = False
        subscribe_task["pause_for_download_time"] = None
        logger.info(f"{self.__format_subscribe(subscribe=subscribe)} 订阅状态已变更，已重置用户新增订阅暂停状态")
        return subscribe_task

    def process_subscribe_pause_for_user(self, subscribe_id: int):
        """
        根据上映日期处理订阅自动暂停
        :param subscribe_id: 订阅标识
        """
        # 如果没有开启自动暂停或没有配置需要暂停的用户
        if not self._auto_pause or not self._auto_pause_users:
            return

        subscribe = self.subscribe_oper.get(sid=subscribe_id)
        if not subscribe:
            logger.error(f"无法找到订阅 ID {subscribe_id} 对应的订阅信息，跳过处理")
            return

        # 检查订阅是否为洗版订阅
        if subscribe.best_version:
            logger.debug(f"{self.__format_subscribe(subscribe)} 为洗版订阅，跳过处理")
            return

        if subscribe.username not in self._auto_pause_users:
            logger.debug(
                f"{self.__format_subscribe(subscribe)} 对应的订阅用户 {subscribe.username} 不在暂停用户列表中，跳过处理")
            return

        self.__with_lock_and_update_subscribe_tasks(method=self.__process_subscribe_pause_for_user,
                                                    subscribe=subscribe)

    def __process_subscribe_pause_for_user(self, subscribe_tasks: dict, subscribe: Subscribe):
        """
        根据用户处理订阅自动暂停
        :param subscribe_tasks: 订阅任务列表
        :param subscribe: 订阅对象
        """
        try:
            previous_state = subscribe.state
            self.subscribe_oper.update(subscribe.id, {"state": "S"})

            subscribe_task, exists = self.__initialize_subscribe_task(subscribe=subscribe,
                                                                      subscribe_tasks=subscribe_tasks)
            subscribe_task["pause_for_user"] = True
            subscribe_task["pause_for_user_time"] = time.time()

            logger.info(f"{self.__format_subscribe(subscribe)} 对应的订阅用户 {subscribe.username} "
                        f"已被设置为新增订阅暂停用户，订阅状态从 {previous_state} 更新为暂停")

            if self._notify:
                self.post_message(
                    mtype=NotificationType.Subscribe,
                    title=f"{self.__format_subscribe_desc(subscribe=subscribe)} 已暂停订阅",
                    text=f"{subscribe.username} 的新增订阅已自动标记为暂停",
                    image=self.__get_subscribe_image(subscribe),
                )
        except Exception as e:
            # 捕获异常并记录错误日志
            logger.error(f"处理订阅 {self.__format_subscribe(subscribe=subscribe)} 时发生错误: {str(e)}", exc_info=True)

    def process_subscribe_pause(self, subscribe_id: Optional[int] = None):
        """
        处理订阅自动暂停
        :param subscribe_id: 订阅标识
        """
        # 如果没有开启自动暂停，则直接返回
        if not self._auto_pause:
            return

        if not subscribe_id:
            subscribes = self.subscribe_oper.list()
        else:
            subscribes = [self.subscribe_oper.get(sid=subscribe_id)]

        if not subscribes:
            return

        self.__with_lock_and_update_subscribe_tasks(method=self.__process_subscribe_pause,
                                                    subscribes=subscribes)

    def __process_subscribe_pause(self, subscribe_tasks: dict, subscribes: list[Subscribe]):
        """
        处理订阅自动暂停
        :param subscribe_tasks: 订阅任务列表
        :param subscribes: 订阅对象列表
        """
        for data in subscribes:
            if isinstance(data, tuple):
                subscribe, mediainfo = data
            else:
                subscribe = data
                mediainfo = None
            try:
                # 检查订阅是否为洗版订阅
                if subscribe.best_version:
                    logger.debug(f"{self.__format_subscribe(subscribe)} 为洗版订阅，跳过处理")
                    continue

                # 获取订阅任务
                subscribe_task, exists = self.__initialize_subscribe_task(subscribe=subscribe,
                                                                          subscribe_tasks=subscribe_tasks)
                # 判断是否已根据用户或下载自动暂停
                is_paused = subscribe_task.get("pause_for_user") or subscribe_task.get("pause_for_download")
                if is_paused:
                    if subscribe.state == "S":
                        logger.info(
                            f"{self.__format_subscribe(subscribe=subscribe)} 已暂停 (用户/下载配置标记)，如需操作，请先启用订阅"
                        )
                        return
                    else:
                        # 重置暂停状态
                        subscribe_task.update({
                            "pause_for_user": False,
                            "pause_for_user_time": None,
                            "pause_for_download": False,
                            "pause_for_download_time": None,
                        })
                        logger.info(
                            f"{self.__format_subscribe(subscribe=subscribe)} 已启用，已重置用户和下载暂停状态。"
                        )

                # 自动识别媒体信息
                if not mediainfo:
                    mediainfo = self.__recognize_media(subscribe)

                if not mediainfo:
                    return

                if mediainfo.type == MediaType.UNKNOWN:
                    logger.info(f"{self.__format_subscribe(subscribe=subscribe)}，未知的媒体类型，跳过处理")
                    return

                execute = self.__process_subscribe_pause_for_download(subscribe_task=subscribe_task,
                                                                      subscribe=subscribe,
                                                                      mediainfo=mediainfo)
                if not execute:
                    self.__process_subscribe_pause_for_airing(subscribe_task=subscribe_task, subscribe=subscribe,
                                                              mediainfo=mediainfo)
            except Exception as e:
                # 捕获异常并记录错误日志
                logger.error(f"处理订阅 {self.__format_subscribe(subscribe=subscribe)} 时发生错误: {str(e)}",
                             exc_info=True)

    def __process_subscribe_pause_for_download(self, subscribe_task: dict, subscribe: Subscribe,
                                               mediainfo: Optional[MediaInfo]) -> bool:
        """
        根据下载信息处理订阅自动暂停
        :param subscribe_task: 订阅任务
        :param subscribe: 订阅信息
        :param mediainfo: 媒体信息
        """
        if not mediainfo:
            return False

        # 检查是否已经配置了无下载天数
        subscribe_type = MediaType(subscribe.type)
        if subscribe_type == MediaType.TV and self._auto_pause_tv_no_download_days is None:
            return False
        if subscribe_type == MediaType.MOVIE and self._auto_pause_movie_no_download_days is None:
            return False

        # 根据媒体类型筛选对应的动作
        if subscribe_type == MediaType.TV:
            relevant_actions = [action for action in self._auto_pause_no_download_actions if action.endswith("_tv")]
        elif subscribe_type == MediaType.MOVIE:
            relevant_actions = [action for action in self._auto_pause_no_download_actions if action.endswith("_movie")]
        else:
            relevant_actions = []

        if not relevant_actions:
            return False

        relevant_action = relevant_actions[0]
        action = relevant_action.split("_")[0]
        action_map = {
            "pause": "暂停",
            "complete": "完成",
            "delete": "删除"
        }
        action_name = action_map.get(action)

        execute, no_download_days = self.__check_subscribe_action_for_download(subscribe=subscribe, mediainfo=mediainfo)
        if not execute:
            return False

        # 如果当前订阅任务需要修改任务状态，则进一步获取订阅任务是否开启剧集待定，如果已开启，则需要重置处理
        tv_pending = subscribe_task.get("tv_pending")
        if tv_pending:
            logger.info(f"{self.__format_subscribe(subscribe)} 已开启剧集待定，订阅状态已发生变更，重置剧集待定")
            self.__update_subscribe_tv_pending_task(subscribe=subscribe,
                                                    subscribe_task=subscribe_task,
                                                    pending=False)

        logger.info(f"{self.__format_subscribe(subscribe=subscribe)} 无下载天数：{no_download_days} 已超过设定值，"
                    f"标记订阅状态为：{action_name}")

        if action == "pause":
            if not subscribe_task.get("pause_for_download"):
                subscribe_task["pause_for_download"] = True
            subscribe_task["pause_for_download_time"] = time.time()
            if subscribe.state != "S":
                self.subscribe_oper.update(sid=subscribe.id, payload={"state": "S"})
        elif action == "complete":
            self.subscribe_oper.add_history(**subscribe.to_dict())
            self.subscribe_oper.delete(subscribe.id)
        elif action == "delete":
            self.subscribe_oper.delete(sid=subscribe.id)

        self.clear_tasks(subscribe_id=subscribe.id, subscribe=subscribe.to_dict())

        meta = self.__get_subscribe_meta(subscribe=subscribe)
        msg_title = f"{mediainfo.title_year} {meta.season} 近 {no_download_days} 天未有下载记录，已标记{action_name}"
        self.__send_subscribe_status_msg(subscribe=subscribe, mediainfo=mediainfo, msg_title=msg_title)
        return True

    def __check_subscribe_action_for_download(self, subscribe: Subscribe, mediainfo: MediaInfo) -> \
            Tuple[bool, Optional[float]]:
        """
       根据下载信息判断订阅需要执行的动作
        :param subscribe: 订阅信息
        :param mediainfo: 媒体信息
        """
        if mediainfo.type == MediaType.UNKNOWN:
            return False, None

        # 根据媒体类型获取对应该类型的无下载天数配置
        no_download_days = (self._auto_pause_tv_no_download_days
                            if mediainfo.type == MediaType.TV else self._auto_pause_movie_no_download_days)

        if no_download_days is None:
            return False, no_download_days

        # 如果是剧集，从订阅与媒体信息中提取对应季数的上映日期
        if mediainfo.type == MediaType.TV:
            # 与当前订阅季数匹配的首映信息
            first_air_date, first_air_day = self.__get_tv_season_air_date(
                mediainfo=mediainfo,
                season=subscribe.season
            )
        else:
            # 电影只需要从 release_date 获取上映信息
            first_air_day = mediainfo.release_date
            first_air_date, first_air_day = self.__parse_date(day=first_air_day)

        current_date = datetime.now()
        if first_air_date is None:
            # 无法正确解析上映日期，不需要执行任何动作
            return False, no_download_days

        # 获取订阅日期
        subscribe_date, subscribe_day = self.__parse_date(day=subscribe.date, f="%Y-%m-%d %H:%M:%S")

        # 获取最后更新日期
        last_update_date, last_update_day = self.__parse_date(day=subscribe.last_update, f="%Y-%m-%d %H:%M:%S")

        # 获取最近下载日期
        downloads = self.__get_related_download_histories(subscribe_id=subscribe.id, subscribe=subscribe)
        last_download_day = max([download.date for download in downloads]) if downloads else None
        last_download_date, last_download_day = self.__parse_date(day=last_download_day, f="%Y-%m-%d %H:%M:%S")

        # 计算无下载的截止日期
        dates = [date for date in [first_air_date, subscribe_date, last_update_date, last_download_date] if date]
        no_download_deadline = max(dates) + timedelta(days=no_download_days)

        logger.debug(f"{self.__format_subscribe(subscribe=subscribe)} "
                     f"订阅日期：{subscribe_day}，上映日期：{first_air_day}，"
                     f"更新日期：{last_update_day}，最近下载日期：{last_download_day}，"
                     f"截止日期：{no_download_deadline.strftime('%Y-%m-%d')}")

        # 如果还没有超过最终日期，说明不需要执行任何动作，否则需要按执行策略进行处理
        return no_download_deadline < current_date, (current_date - max(dates)).days

    def __process_subscribe_pause_for_airing(self, subscribe_task: dict, subscribe: Subscribe,
                                             mediainfo: MediaInfo):
        """
        根据播出信息处理订阅自动暂停
        :param subscribe_task: 订阅任务
        :param subscribe: 订阅信息
        :param mediainfo: 媒体信息
        """
        if not mediainfo:
            return

        # 检查是否已经配置了暂停天数
        subscribe_type = MediaType(subscribe.type)
        if subscribe_type == MediaType.TV and (
                self._auto_pause_tv_air_days is None or self._auto_pause_tv_latest_days is None):
            return
        if subscribe_type == MediaType.MOVIE and self._auto_pause_movie_no_download_days is None:
            return

        pause, air_day, reason = self.__check_subscribe_pause_for_airing(subscribe=subscribe,
                                                                         mediainfo=mediainfo)

        # 如果当前状态为 "S"，且需要启用处理，则触发补全搜索
        if subscribe.state == "S" and not pause:
            random_minutes = random.uniform(3, 5)
            logger.info(f"{self.__format_subscribe(subscribe)}，启用订阅，触发补全搜索任务，"
                        f"任务将在 {random_minutes:.2f} 分钟后触发")
            timer = threading.Timer(random_minutes * 60,
                                    lambda sid=subscribe.id: SubscribeChain().search(sid=sid))
            timer.start()

        target_state = subscribe.state
        if pause and subscribe.state != "S":
            target_state = "S"
        elif not pause and subscribe.state == "S":
            target_state = "R"

        if subscribe.state == target_state:
            return

        # 更新订阅状态
        logger.info(f"{self.__format_subscribe(subscribe)} 订阅状态从 {subscribe.state} 更新为 {target_state}")
        self.subscribe_oper.update(subscribe.id, {"state": target_state})

        # 如果当前订阅任务需要修改任务状态，则进一步获取订阅任务是否开启剧集待定，如果已开启，则需要重置处理
        tv_pending = subscribe_task.get("tv_pending")
        if tv_pending:
            logger.info(f"{self.__format_subscribe(subscribe)} 已开启剧集待定，订阅状态已发生变更，重置剧集待定")
            self.__update_subscribe_tv_pending_task(subscribe=subscribe,
                                                    subscribe_task=subscribe_task,
                                                    pending=False)

        # 构造标题，根据状态动态调整
        meta = self.__get_subscribe_meta(subscribe=subscribe)
        if pause:
            msg_title = f"{mediainfo.title_year} {meta.season} {reason}满足订阅暂停，已标记暂停"
        else:
            msg_title = f"{mediainfo.title_year} {meta.season} {reason}不再满足订阅暂停，已标记订阅中"

        self.__send_subscribe_status_msg(subscribe=subscribe, mediainfo=mediainfo,
                                         air_day=f"{reason}：{air_day}", msg_title=msg_title)

    def __check_subscribe_pause_for_airing(self, subscribe: Subscribe, mediainfo: MediaInfo) \
            -> Tuple[bool, Optional[str], Optional[str]]:
        """
        根据播出信息判断订阅是否为暂停
        :param subscribe: 订阅信息
        :param mediainfo: 媒体信息
        :return: (pause: bool, air_day: Optional[str])
            pause: True 表示满足暂停条件
            air_day: 日期
            reason: 原因
        """
        if mediainfo.type == MediaType.UNKNOWN:
            return False, None, None

        # 根据媒体类型获取对应该类型的自动暂停天数配置
        auto_pause_days = (
            self._auto_pause_tv_air_days if mediainfo.type == MediaType.TV else self._auto_pause_movie_air_days
        )

        # 如果是剧集，从订阅与媒体信息中提取对应季数的上映日期
        if mediainfo.type == MediaType.TV:
            # 与当前订阅季数匹配的首映信息
            first_air_date, first_air_day = self.__get_tv_season_air_date(
                mediainfo=mediainfo,
                season=subscribe.season
            )
        else:
            # 电影只需要从 release_date 获取上映信息
            first_air_day = mediainfo.release_date
            first_air_date, first_air_day = self.__parse_date(day=first_air_day)

        # 默认不暂停，即将播出日期为上映日期，如果是剧集，还需要进一步处理季中最终集的场景
        pause = False
        reason = "未知"
        latest_air_day = "未知"
        air_day = first_air_day or "未知"
        current_date = datetime.now()

        # 条件1：基于上映日期的自动暂停，若配置了自动暂停天数，则依据上映日期的时间进行判断
        if auto_pause_days is not None:
            reason = "上映日期"
            if first_air_date is None:
                # 无法正确解析上映日期，默认视为需要暂停
                pause = True
                air_day = "未知"
            else:
                # 若上映日期-配置天数比当前时间还晚，说明尚未到达可播放时间
                pending_date = first_air_date - timedelta(days=auto_pause_days)
                pause = pending_date > current_date

        # 条件2：基于即将播出集的自动暂停，若配置了自动暂停天数，则继续判断
        if not pause and mediainfo.type == MediaType.TV and self._auto_pause_tv_latest_days is not None:
            reason = "即将播出日期"
            air_day = "未知"
            latest_episode, next_episode = self.__get_tv_latest_episode(
                mediainfo=mediainfo,
                season=subscribe.season
            )
            if latest_episode:
                latest_ep_date, latest_air_day = self.__parse_date(day=latest_episode.air_date)
                if next_episode and next_episode.air_date:
                    # 如果下集尚未下载，并当前最新集为已下载的最大集
                    downloads = subscribe.note or []
                    start_episode = subscribe.total_episode - subscribe.lack_episode + 1
                    next_ep_date, air_day = self.__parse_date(day=next_episode.air_date)
                    logger.debug(
                        f"{self.__format_subscribe(subscribe=subscribe)} 即将下载集：{start_episode}，已下载集数：{downloads}")
                    if (
                            next_episode.episode_number == start_episode
                            and next_episode.episode_number not in downloads
                            # and latest_episode.episode_number in downloads
                    ):
                        if next_ep_date:
                            pause = next_ep_date - current_date > timedelta(days=self._auto_pause_tv_latest_days)

        logger.debug(f"{self.__format_subscribe(subscribe)}，pause: {pause}，上映日期: {first_air_day}，"
                     f"最近播出日期：{latest_air_day}，即将播出日期：{air_day}，"
                     f"上映暂停天数：{auto_pause_days}，即将播出暂停天数：{self._auto_pause_tv_latest_days}，"
                     f"当前日期: {current_date.strftime('%Y-%m-%d %H:%M:%S')}")

        return pause, air_day, reason

    def process_tv_pending(self, subscribe_id: Optional[int] = None):
        """
        处理剧集自动待定
        :param subscribe_id: 订阅标识
        """
        if not self._auto_tv_pending or (self._auto_tv_pending_days is None and self._auto_tv_pending_episodes is None):
            return

        if not subscribe_id:
            subscribes = self.subscribe_oper.list(state="N,R,P")
        else:
            subscribes = [self.subscribe_oper.get(sid=subscribe_id)]

        if not subscribes:
            return

        self.__with_lock_and_update_subscribe_tasks(method=self.__process_tv_pending, subscribes=subscribes)

    def __process_tv_pending(self, subscribe_tasks: dict, subscribes: list[Subscribe]):
        """
        处理剧集自动待定
        :param subscribe_tasks: 订阅任务字典
        :param subscribes: 订阅对象列表
        """
        for data in subscribes:
            if isinstance(data, tuple):
                subscribe, mediainfo = data
            else:
                subscribe = data
                mediainfo = None
            try:
                # 检查订阅是否为洗版订阅
                if subscribe.best_version:
                    logger.debug(f"{self.__format_subscribe(subscribe)} 为洗版订阅，跳过处理")
                    continue

                # 检查订阅状态是否可处理
                if not self.__check_subscribe_status(subscribe=subscribe):
                    continue

                # 检查订阅类型是否为剧集
                if subscribe.type != MediaType.TV.value:
                    logger.debug(f"{subscribe.name} 的类型为 {subscribe.type}，非 TV 类型，跳过处理")
                    continue

                # 自动识别媒体信息
                if not mediainfo:
                    mediainfo = self.__recognize_media(subscribe)

                if not mediainfo:
                    continue

                # 检查媒体类型是否为 TV
                if mediainfo.type != MediaType.TV:
                    logger.debug(
                        f"{self.__format_subscribe(subscribe)} 类型为 {mediainfo.type}，非 TV 类型，跳过处理")
                    continue

                # 检查季信息是否存在
                if not mediainfo.season_info:
                    logger.warning(f"{self.__format_subscribe(subscribe)} 的 season_info 为空，跳过处理")
                    continue

                tv_pending, air_day = self.__check_tv_pending_by_mediainfo(subscribe=subscribe, mediainfo=mediainfo)

                # 如果当前状态为 "N"，且需要待定处理，则触发补全搜索
                if subscribe.state == "N" and tv_pending:
                    random_minutes = random.uniform(3, 5)
                    logger.info(f"{self.__format_subscribe(subscribe)}，新增订阅触发补全搜索任务，"
                                f"任务将在 {random_minutes:.2f} 分钟后触发")
                    timer = threading.Timer(random_minutes * 60,
                                            lambda sid=subscribe.id: SubscribeChain().search(sid=sid))
                    timer.start()

                subscribe_task, exists = self.__initialize_subscribe_task(subscribe=subscribe,
                                                                          subscribe_tasks=subscribe_tasks)

                # 更新订阅待定状态
                updated = self.__update_subscribe_tv_pending_task(subscribe=subscribe,
                                                                  subscribe_task=subscribe_task,
                                                                  pending=tv_pending)

                # 更新订阅状态，如果订阅任务没有被其他场景待定，则这里使用目标状态，如果已被其他场景修改，则这里使用待定状态更新
                pending = self.__get_subscribe_task_pending(subscribe_task=subscribe_task)
                target_state = subscribe.state

                if pending and subscribe.state != "P":
                    target_state = "P"
                elif not pending and subscribe.state == "P":
                    target_state = "R"

                # 构造标题，根据状态动态调整
                meta = self.__get_subscribe_meta(subscribe=subscribe)
                if tv_pending:
                    msg_title = f"{mediainfo.title_year} {meta.season} 满足上映待定，已标记待定"
                else:
                    msg_title = f"{mediainfo.title_year} {meta.season} 不再满足上映待定，已标记订阅中"

                if subscribe.state == target_state:
                    # 如果订阅目标状态一致，但是订阅待定状态已变更，需要更新订阅集数以及推送消息
                    if updated:
                        episode_count = self.__update_tv_pending_episodes(subscribe=subscribe, mediainfo=mediainfo,
                                                                          tv_pending=tv_pending)
                        self.__send_subscribe_status_msg(subscribe=subscribe, mediainfo=mediainfo,
                                                         air_day=f"上映日期：{air_day}", msg_title=msg_title,
                                                         episode_count=episode_count)
                    continue

                logger.info(f"{self.__format_subscribe(subscribe)} 订阅状态从 {subscribe.state} 更新为 {target_state}")
                self.subscribe_oper.update(subscribe.id, {"state": target_state})

                if updated:
                    episode_count = self.__update_tv_pending_episodes(subscribe=subscribe, mediainfo=mediainfo,
                                                                      tv_pending=tv_pending)
                    self.__send_subscribe_status_msg(subscribe=subscribe, mediainfo=mediainfo,
                                                     air_day=f"上映日期：{air_day}", msg_title=msg_title,
                                                     episode_count=episode_count)
            except Exception as e:
                # 捕获异常并记录错误日志
                logger.error(f"处理订阅 {self.__format_subscribe(subscribe=subscribe)} 时发生错误: {str(e)}",
                             exc_info=True)

    def __update_tv_pending_episodes(self, subscribe: Subscribe, mediainfo: MediaInfo, tv_pending: bool) \
            -> Optional[int]:
        """
        更新待定剧集总集数
        """
        if self._auto_update_tv_pending_episodes is None:
            return None

        # 初始化更新字段
        update_data = {"manual_total_episode": 1 if tv_pending else 0}

        if tv_pending:
            episode_count = int(self._auto_update_tv_pending_episodes)
        else:
            episodes = mediainfo.seasons.get(subscribe.season)
            episode_count = len(episodes) if episodes else 0
        lack_episode = subscribe.lack_episode + (episode_count - subscribe.total_episode)

        # 如果 episode_count 不为空，则添加到更新字段
        if episode_count:
            update_data["total_episode"] = episode_count
            update_data["lack_episode"] = lack_episode

        # 更新订阅信息
        logger.info(f"{self.__format_subscribe(subscribe=subscribe)} 待定状态：{tv_pending}，"
                    f"手动更新集数状态：{tv_pending}，总集数更新为：{episode_count}，缺失集数更新为：{lack_episode}")
        self.subscribe_oper.update(subscribe.id, update_data)
        return episode_count

    def __check_tv_pending_by_mediainfo(self, subscribe: Subscribe, mediainfo: MediaInfo) -> Tuple[bool, Optional[str]]:
        """
        根据媒体信息判断剧集订阅是否为待定
        :param subscribe: 订阅信息
        :param mediainfo: 媒体信息
        """
        # 查找与当前订阅季数匹配的上映日期 (air_date)
        air_date, air_day = self.__get_tv_season_air_date(mediainfo=mediainfo, season=subscribe.season)

        # 查询与当前订阅季数匹配的剧集总数 (episode_count)
        episode_count = self.__get_tv_season_episode_count(mediainfo=mediainfo, season=subscribe.season)

        # 获取剧集是否已完结
        completed = self.__check_tv_season_completed(mediainfo=mediainfo, season=subscribe.season)

        tv_pending = False
        current_date = datetime.now()

        # 只有剧集没有完结时，才需要考虑是否待定
        if not completed:
            # 条件1：配置了剧集待定天数，并且存在上映日期，且满足待定条件
            condition_days = False
            if self._auto_tv_pending_days is not None and air_date:
                pending_date = air_date + timedelta(days=self._auto_tv_pending_days)
                condition_days = pending_date > current_date

            # 条件2：配置了剧集待定集数，并且存在集数，且满足待定条件
            condition_episodes = False
            if self._auto_tv_pending_episodes is not None and episode_count is not None:
                condition_episodes = episode_count <= self._auto_tv_pending_episodes

            # 任一条件成立，则 tv_pending 为 True
            tv_pending = condition_days or condition_episodes

        logger.debug(
            f"{self.__format_subscribe(subscribe)}，tv_pending: {tv_pending}，完结状态：{completed}，"
            f"上映日期: {air_day}，剧集数：{episode_count}，待定天数：{self._auto_tv_pending_days}，"
            f"当前日期: {current_date.strftime('%Y-%m-%d %H:%M:%S')}")

        return tv_pending, air_day

    def __send_subscribe_status_msg(self, subscribe: Subscribe, mediainfo: MediaInfo, msg_title: str,
                                    air_day: Optional[str] = None, episode_count: Optional[int] = None):
        """
        发送订阅状态消息
        :param subscribe: 订阅信息
        :param mediainfo: 媒体信息
        :param msg_title: 消息标题
        :param air_day: 日期
        :param episode_count: 集数
        """
        if not self._notify:
            return

        # 构造消息文本
        text_parts = []
        if mediainfo.vote_average:
            text_parts.append(f"评分：{mediainfo.vote_average}")
        if subscribe.username:
            text_parts.append(f"来自用户：{subscribe.username}")
        if air_day:
            text_parts.append(air_day)
        if episode_count:
            text_parts.append(f"集数更新为：{episode_count}")
        # 将非空部分拼接成完整的文本
        text = "，".join(text_parts) if text_parts else ""

        # 构造跳转链接
        if mediainfo.type == MediaType.TV:
            link = settings.MP_DOMAIN('#/subscribe/tv?tab=mysub')
        else:
            link = settings.MP_DOMAIN('#/subscribe/movie?tab=mysub')

        self.post_message(
            mtype=NotificationType.Subscribe,
            title=msg_title,
            text=text,
            image=mediainfo.get_message_image(),
            link=link,
            # username=subscribe.username
        )

    def __recognize_media(self, subscribe: Subscribe) -> Optional[MediaInfo]:
        """
        识别媒体信息
        param subscribe: 订阅对象
        """
        meta = MetaInfo(subscribe.name)
        meta.year = subscribe.year
        meta.begin_season = subscribe.season or None
        try:
            meta.type = MediaType(subscribe.type)
        except ValueError:
            logger.error(f"订阅 {subscribe.name} 类型错误：{subscribe.type}", exc_info=True)
            return None
        try:
            # 识别媒体信息
            mediainfo: MediaInfo = self.chain.recognize_media(
                meta=meta,
                mtype=meta.type,
                tmdbid=subscribe.tmdbid,
                doubanid=subscribe.doubanid,
                cache=False
            )
            if not mediainfo:
                logger.warning(
                    f"未识别到媒体信息，标题：{subscribe.name}，tmdbid：{subscribe.tmdbid}，doubanid：{subscribe.doubanid}")
                return None
            return mediainfo
        except Exception as e:
            logger.error(f"识别媒体信息时发生错误，订阅 ID {subscribe.id}，标题：{subscribe.name}，错误信息：{str(e)}",
                         exc_info=True)
            return None

    def __get_data(self, key: str) -> dict:
        """
        获取插件数据
        """
        return self.get_data(key=key) or {}

    def __save_data(self, key: str, value: Any) -> dict:
        """
        保存插件数据
        """
        return self.save_data(key=key, value=value)

    @staticmethod
    def __match_subscribe(subscribe: Subscribe, subscribe_task: dict) -> bool:
        """
        判断是否为同一个订阅
        """
        # 如果不存在或为空，则返回 False
        if not subscribe or not subscribe_task:
            return False

        # 判断ID
        if subscribe.id != subscribe_task.get("id") or subscribe.name != subscribe_task.get("name"):
            return False

        # 判断 tmdbid
        if subscribe.tmdbid and subscribe_task.get("tmdbid") != subscribe.tmdbid:
            return False

        # 判断 doubanid
        if subscribe.doubanid and subscribe_task.get("doubanid") != subscribe.doubanid:
            return False

        return True

    @staticmethod
    def __format_subscribe(subscribe: Subscribe) -> str:
        """
        格式化订阅信息
        """
        if not subscribe:
            return "无效的订阅信息"

        # 基于订阅类型拼接不同的字符串格式
        mediatype = MediaType(subscribe.type)
        year = subscribe.year if subscribe.year else "Unknown"
        if mediatype == MediaType.TV:
            return f"剧集: {subscribe.name} ({year}) 季{subscribe.season} [{subscribe.id}]"
        elif mediatype == MediaType.MOVIE:
            return f"电影: {subscribe.name} ({year}) [{subscribe.id}]"
        else:
            return f"未知类型: {subscribe.name} ({year}) [{subscribe.id}]"

    def __format_subscribe_desc(self, subscribe: Optional[Subscribe] = None, mediainfo: Optional[MediaInfo] = None) -> \
            Optional[str]:
        """
        格式化订阅描述信息
        """
        if not subscribe:
            return None

        if not mediainfo:
            mediainfo = self.__recognize_media(subscribe=subscribe)

        if mediainfo:
            meta = MetaInfo(subscribe.name)
            meta.year = subscribe.year
            meta.begin_season = subscribe.season or None
            meta.type = mediainfo.type

            subscribe_desc = f"{mediainfo.title_year} {meta.season}" \
                if mediainfo.type == MediaType.TV else f"{mediainfo.title_year}"
            return subscribe_desc
        else:
            self.__format_subscribe(subscribe=subscribe)

    @staticmethod
    def __compare_torrent_info_and_task(torrent_info: TorrentInfo, torrent_task: dict,
                                        partial_match: bool = False) -> bool:
        """
        判断 torrent_info 和 task 是否一致
        :param torrent_info: TorrentInfo 实例
        :param torrent_task: 任务字典
        :param partial_match: 是否启用部分匹配
        :return: 如果一致返回 True，不一致返回 False
        """
        if not torrent_info or not torrent_task:
            return False

        def is_match(field1, field2):
            """
            检查两个字段是否匹配
            :param field1: 第一个字段
            :param field2: 第二个字段
            :return: 是否匹配
            """
            if partial_match:
                return field1 and field2 and (field1 in field2 or field2 in field1)
            return field1 == field2

        # 检查 enclosure 和 page_url 的匹配
        if is_match(torrent_info.enclosure, torrent_task.get("enclosure")):
            return True

        if is_match(torrent_info.page_url, torrent_task.get("page_url")):
            return True

        # 如果都没有匹配到，返回 False
        return False

    def clear_tasks(self, subscribe_id: int, subscribe: dict):
        """
        清理任务
        :param subscribe_id: 订阅 ID
        :param subscribe: 订阅信息
        """
        self.__with_lock_and_update_subscribe_tasks(
            method=self.__clear_subscribe_tasks, subscribe_id=subscribe_id
        )
        self.__with_lock_and_update_torrent_tasks(
            method=self.__clear_torrent_tasks, subscribe_id=subscribe_id
        )

    @staticmethod
    def __clear_subscribe_tasks(subscribe_tasks: dict, subscribe_id: int):
        """
        清理订阅任务
        :param subscribe_tasks: 订阅任务字典
        :param subscribe_id: 订阅 ID
        """
        subscribe_id = str(subscribe_id)
        subscribe_tasks.pop(subscribe_id, None)

    @staticmethod
    def __clear_torrent_tasks(torrent_tasks: dict, subscribe_id: int):
        """
        清理种子任务
        :param torrent_tasks: 种子任务字典
        :param subscribe_id: 订阅 ID
        """
        for k in list(torrent_tasks.keys()):
            if torrent_tasks[k].get("subscribe_id") == subscribe_id:
                del torrent_tasks[k]

    @staticmethod
    def __update_or_add_delete_tasks(delete_tasks: dict, torrent_task: dict):
        """
        更新已删除种子任务
        :param delete_tasks: 已删除种子任务
        :param torrent_task: 种子任务
        """
        if not torrent_task:
            return
        torrent_hash = torrent_task.get("hash")
        torrent_task["delete_time"] = time.time()
        delete_tasks[torrent_hash] = torrent_task

    def __update_subscribe_torrent_task(self, subscribe_tasks: dict, subscribe: Subscribe,
                                        torrent_hash: Optional[str] = None,
                                        torrent_info: Optional[TorrentInfo] = None, episodes: list[int] = None,
                                        downloader: str = None, pending: bool = False,
                                        update_priority=False) -> Optional[dict]:
        """
        更新订阅种子任务，支持移除完成任务、更新或新增种子任务
        :param subscribe_tasks: 订阅任务字典
        :param subscribe: 订阅对象
        :param torrent_hash: 可选，种子的 hash 值
        :param torrent_info: 可选，种子信息
        :param episodes: 可选，需要下载的集数
        :param downloader: 可选，下载器
        :param pending: 可选，是否将种子任务标记为待定
        :param update_priority：可选，更新优先级
        :return: 返回更新后的订阅任务对象，或者移除任务后的任务信息
        """
        if not subscribe or subscribe_tasks is None:
            return None

        # 获取或初始化订阅任务
        subscribe_task, exists = self.__initialize_subscribe_task(subscribe, subscribe_tasks)

        # 更新或新增种子任务
        self.__update_or_add_subscribe_torrent_task(subscribe_task, torrent_hash, torrent_info,
                                                    episodes, downloader, pending)

        # 更新优先级
        if update_priority:
            subscribe_task["current_priority"] = subscribe.current_priority

        return subscribe_task

    def __update_or_add_subscribe_torrent_task(self, subscribe_task: dict, torrent_hash: Optional[str] = None,
                                               torrent_info: Optional[TorrentInfo] = None,
                                               episodes: list[int] = None,
                                               downloader: str = None,
                                               pending: bool = False) -> bool:
        """
        更新或新增订阅种子任务
        :param subscribe_task: 订阅任务
        :param torrent_hash: 种子hash
        :param torrent_info: 种子数据
        :param episodes: 需要下载的集数
        :param downloader: 下载器
        :param pending: 是否待定
        """
        if not subscribe_task:
            return False

        torrent_tasks = subscribe_task.setdefault("torrent_tasks", [])
        for task in torrent_tasks:
            if torrent_hash:
                # 如果已经有相同的 torrent_hash，直接返回
                if task.get("hash") == torrent_hash:
                    return False
                # 如果任务没有 hash 且信息匹配，更新 hash
                if not task.get("hash") and self.__compare_torrent_info_and_task(torrent_info, task):
                    task.update({
                        "hash": torrent_hash,
                        "episodes": episodes,
                        "downloader": downloader
                    })
                    return True
            else:
                if self.__compare_torrent_info_and_task(torrent_info, task):
                    return False

        if not torrent_info:
            return False

        # 如果未找到匹配任务，初始化一个新的 torrent_task
        torrent_tasks.append({
            "hash": torrent_hash,
            "site_id": torrent_info.site,
            "site_name": torrent_info.site_name,
            "title": torrent_info.title,
            "description": torrent_info.description,
            "enclosure": torrent_info.enclosure,
            "page_url": torrent_info.page_url,
            "episodes": episodes,
            "downloader": downloader,
            "time": time.time(),
            "pending": pending,
            "pending_time": time.time() if pending else None
        })
        return True

    def __update_subscribe_tv_pending_task(self, subscribe: Subscribe, subscribe_task: dict,
                                           pending: bool = False) -> bool:
        """
        更新订阅任务剧集待定状态
        :param subscribe: 订阅对象
        :param subscribe_task: 订阅任务
        :param pending: 是否设置为剧集待定
        """
        if not subscribe or subscribe_task is None:
            return False

        if subscribe_task.get("tv_pending", False) == pending:
            logger.debug(f"{self.__format_subscribe(subscribe)} 当前订阅剧集待定状态无需变更")
            return False

        # 更新 tv_pending 状态
        if pending:
            logger.debug(f"{self.__format_subscribe(subscribe)} 当前订阅剧集待定状态更新为待定")
            subscribe_task["tv_pending"] = True
            subscribe_task["tv_pending_time"] = time.time()
        else:
            logger.debug(f"{self.__format_subscribe(subscribe)} 当前订阅剧集待定状态更新为订阅中")
            subscribe_task["tv_pending"] = False
            subscribe_task["tv_pending_time"] = None

        return True

    def __get_subscribe_task_pending(self, subscribe_task: dict) -> bool:
        """
        获取待定状态
        :param subscribe_task: 订阅任务
        """
        if not subscribe_task:
            return False

        if subscribe_task.get("tv_pending"):
            return True

        return self.__get_subscribe_task_download_pending(subscribe_task=subscribe_task)

    @staticmethod
    def __get_subscribe_task_download_pending(subscribe_task: dict) -> bool:
        """
        获取待定状态
        :param subscribe_task: 订阅任务
        """
        if not subscribe_task:
            return False

        for task in subscribe_task.get("torrent_tasks", []):
            if task.get("hash") and task.get("pending"):
                return True

        return False

    def __initialize_subscribe_task(self, subscribe: Subscribe, subscribe_tasks: dict) -> tuple[dict, bool]:
        """
        初始化订阅任务，或者获取已有的订阅任务
        :param subscribe: 订阅对象
        :param subscribe_tasks: 订阅任务列表
        :return: 订阅任务，是否已存在
        """
        subscribe_id = str(subscribe.id)
        subscribe_task = subscribe_tasks.get(subscribe_id)

        # 判断现有任务是否存在且一致
        if subscribe_task:
            match = self.__match_subscribe(subscribe=subscribe, subscribe_task=subscribe_task)
            if match:
                return subscribe_task, True
            else:
                # 订阅信息不一致，记录日志并删除旧的订阅任务
                logger.info(f"订阅任务不一致，删除原任务：ID={subscribe_id}, Name={subscribe_task.get('name')}, "
                            f"Subscribe_task={subscribe_task}")
                subscribe_tasks.pop(subscribe_id)

        # 创建新的订阅任务
        subscribe_task = {
            "id": subscribe.id,
            "name": subscribe.name,
            "year": subscribe.year,
            "type": subscribe.type,
            "season": subscribe.season,
            "tmdbid": subscribe.tmdbid,
            "imdbid": subscribe.imdbid,
            "tvdbid": subscribe.tvdbid,
            "doubanid": subscribe.doubanid,
            "bangumiid": subscribe.bangumiid,
            "best_version": subscribe.best_version,
            "current_priority": subscribe.current_priority,
            "pause_for_user": False,
            "pause_for_user_time": None,
            "pause_for_download": False,
            "pause_for_download_time": None,
            "tv_pending": False,
            "tv_pending_time": None,
            "torrent_tasks": []
        }
        subscribe_tasks[subscribe_id] = subscribe_task
        return subscribe_task, False

    @staticmethod
    def __get_subscribe_image(subscribe: Subscribe):
        """
        返回订阅图片地址
        """
        if subscribe.backdrop:
            return subscribe.backdrop.replace("original", "w500")
        if subscribe.poster:
            return subscribe.poster.replace("original", "w500")
        return ""

    @staticmethod
    def __get_subscribe_meta(subscribe: Subscribe) -> MetaInfo:
        """
        获取订阅元数据
        """
        meta = MetaInfo(subscribe.name)
        meta.year = subscribe.year
        meta.begin_season = subscribe.season or None
        meta.type = MediaType(subscribe.type)
        return meta

    def process_best_version_complete(self, subscribes: list[Subscribe]):
        """
        处理自动洗版完成检查
        :param subscribes: 订阅对象列表
        """
        if not self._auto_best_types or not subscribes:
            return

        if not self._auto_best_remaining_days:
            logger.debug("未配置洗版天数，跳过处理")
            return

        if self._auto_best_remaining_days <= 0:
            logger.debug("洗版天数小于等于0，跳过处理")
            return

        for subscribe in subscribes:
            if not subscribe.best_version:
                continue

            # 优先级已经是洗版完成，跳过
            if subscribe.current_priority == 100:
                logger.debug(f"{self.__format_subscribe(subscribe)} 优先级已标识为洗版完成，跳过处理")
                continue

            # 获取最后更新的日期，优先使用 last_update，否则使用创建日期
            last_update_date_str = subscribe.last_update or subscribe.date
            if not last_update_date_str:
                logger.debug(f"{self.__format_subscribe(subscribe)} 没有有效的日期，跳过处理")
                continue

                # 将字符串转换为 datetime 对象
            try:
                last_update_date = datetime.strptime(last_update_date_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                # 如果日期格式不匹配，跳过此条订阅
                logger.warning(f"{self.__format_subscribe(subscribe)} 的日期格式不匹配，跳过处理")
                continue

            # 计算距离当前的天数
            remaining_days = (datetime.now() - last_update_date).total_seconds() / 86400
            logger.info(f"{self.__format_subscribe(subscribe)} 距离上次更新 {remaining_days:.2f} 天")

            if remaining_days >= self._auto_best_remaining_days:
                # 如果剩余天数已大于洗版天数，则更新优先级为100，标识为洗版完成
                logger.info(f"{self.__format_subscribe(subscribe)} 已满足洗版天数，更新优先级为 100")
                self.subscribe_oper.update(sid=subscribe.id, payload={"current_priority": 100})
            else:
                logger.info(f"订阅 {self.__format_subscribe(subscribe)} 尚未满足洗版天数，跳过处理")

    def process_best_version(self, subscribe_dict: dict, mediainfo: MediaInfo):
        """
        处理自动洗版
        """
        if not subscribe_dict:
            return

        subscribe_id = subscribe_dict.pop("id", None)
        model_fields = SchemaSubscribe.__fields__
        for key in list(subscribe_dict.keys()):
            if key not in model_fields:
                subscribe_dict.pop(key)
        subscribe = SchemaSubscribe(**subscribe_dict)

        if subscribe.best_version:
            logger.debug(f"{self.__format_subscribe(subscribe)} 已为洗版订阅，跳过处理")
            return

        # 如果订阅类型不在自动洗版的策略中，则直接返回
        subscribe_type = MediaType(subscribe.type)
        if subscribe_type not in self._auto_best_types:
            logger.debug(f"{self.__format_subscribe(subscribe)}，尚未开启自动洗版，跳过处理")
            return

        # 如果是分集下载的洗版，则判断下载记录是否存在多条记录
        if subscribe_type == MediaType.TV and "tv_episode" == self._auto_best_type:
            downloads = self.__get_related_download_histories(subscribe_id=subscribe_id, subscribe=subscribe)
            if not downloads:
                logger.info(f"{self.__format_subscribe(subscribe)}，没有下载记录，跳过处理")
                return

            download_count = len(downloads)
            if download_count <= 1:
                logger.info(
                    f"{self.__format_subscribe(subscribe)}，关联下载记录共 {download_count} 条，不是分集下载订阅，跳过处理")
                return

        # 自动识别媒体信息
        if not mediainfo:
            mediainfo = self.__recognize_media(subscribe)

        if not mediainfo:
            return

        # 更新订阅字典
        subscribe_dict["best_version"] = True
        subscribe_dict["username"] = self.plugin_name
        subscribe_dict["state"] = "N"
        fields_to_pop = [
            "name", "year", "type", "tmdbid", "imdbid", "tvdbid", "doubanid", "bangumiid",
            "poster", "backdrop", "vote", "description", "date", "last_update", "note", "state", "current_priority"
        ]
        for field in fields_to_pop:
            subscribe_dict.pop(field, None)
        if mediainfo.type == MediaType.TV:
            subscribe_dict["lack_episode"] = subscribe_dict.get("total_episode")

        # 添加订阅
        sid, err_msg = self.subscribe_oper.add(mediainfo=mediainfo,
                                               **subscribe_dict)

        subscribe_desc = self.__format_subscribe_desc(subscribe=subscribe, mediainfo=mediainfo)

        if sid:
            logger.info(f"{subscribe_desc} 已成功添加洗版订阅 (ID: {sid})")
            # 发送事件
            eventmanager.send_event(EventType.SubscribeAdded, {
                "subscribe_id": sid,
                "username": self.plugin_name,
                "mediainfo": mediainfo.to_dict(),
            })
        else:
            logger.error(f"{subscribe_desc} 添加洗版订阅失败，错误信息: {err_msg}", exc_info=True)

        if not self._notify:
            return

        if not sid:
            self.post_message(
                mtype=NotificationType.Subscribe,
                title=f"{subscribe_desc} 添加洗版订阅失败！",
                text=err_msg,
                image=mediainfo.get_message_image()
            )
        else:
            if mediainfo.type == MediaType.TV:
                link = settings.MP_DOMAIN('#/subscribe/tv?tab=mysub')
            else:
                link = settings.MP_DOMAIN('#/subscribe/movie?tab=mysub')
            self.post_message(
                mtype=NotificationType.Subscribe,
                title=f"{subscribe_desc} 已添加洗版订阅",
                text=f"评分：{mediainfo.vote_average}，来自用户：{self.plugin_name}",
                image=mediainfo.get_message_image(),
                link=link,
                # username=subscribe.username
            )

    def __with_lock_and_update_subscribe_tasks(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """
        使用锁获取并更新订阅任务数据
        :param method: 需要执行的操作，接收当前数据字典并进行修改
        :param *args: 额外的位置参数
        :param **kwargs: 额外的关键字参数
        """
        with lock:
            try:
                # 获取数据
                tasks = self.__get_data(key="subscribes")

                # 执行需要的操作
                method(tasks, *args, **kwargs)

                # 保存修改后的数据
                self.__save_data(key="subscribes", value=tasks)
            except Exception as e:
                # 处理异常
                logger.error(f"Error during {method.__name__}: {e}", exc_info=True)

    def __with_lock_and_update_torrent_tasks(self, method: Callable[..., None], *args: Any, **kwargs: Any) -> None:
        """
        使用锁获取并更新下载任务数据
        :param method: 需要执行的操作，接收当前数据字典并进行修改
        :param *args: 额外的位置参数
        :param **kwargs: 额外的关键字参数
        """
        with lock:
            try:
                # 获取数据
                tasks = self.__get_data(key="torrents")

                # 执行需要的操作
                method(tasks, *args, **kwargs)

                # 保存修改后的数据
                self.__save_data(key="torrents", value=tasks)
            except Exception as e:
                # 处理异常
                logger.error(f"Error during {method.__name__}: {e}", exc_info=True)

    def __with_lock_and_update_delete_tasks(self, method: Callable[..., None], *args: Any, **kwargs: Any) -> None:
        """
        使用锁获取并更新删除任务数据
        :param method: 需要执行的操作，接收当前数据字典并进行修改
        :param *args: 额外的位置参数
        :param **kwargs: 额外的关键字参数
        """
        with lock:
            try:
                # 获取数据
                tasks = self.__get_data(key="deletes")

                # 执行需要的操作
                method(tasks, *args, **kwargs)

                # 保存修改后的数据
                self.__save_data(key="deletes", value=tasks)
            except Exception as e:
                # 处理异常
                logger.error(f"Error during {method.__name__}: {e}", exc_info=True)

    def __check_subscribe_status(self, subscribe: Subscribe) -> bool:
        """
        检查订阅状态是否符合要求
        :param subscribe: 订阅信息
        """
        if not subscribe:
            return False

        # 检查订阅状态是否可处理
        if subscribe.state not in ["N", "R", "P"]:
            logger.debug(
                f"{self.__format_subscribe(subscribe)} 当前状态为 {subscribe.state}，状态不允许处理，跳过处理")
            return False
        return True

    def __check_tv_season_completed(self, mediainfo: MediaInfo, season: int) -> bool:
        """
        按季判断剧集是否已完结
        :param mediainfo: 媒体信息
        :param season: 季数
        """
        if not mediainfo or not mediainfo.tmdb_id or not season:
            return False

        # 如果剧集状态为 Ended 或 Canceled，则认为已完结
        if mediainfo.status in ["Ended", "Canceled"]:
            return True

        episodes = self.tmdb_chain.tmdb_episodes(tmdbid=mediainfo.tmdb_id, season=season)
        if not episodes:
            return False

        # 判断是否存在最终集，存在则认为已完结
        completed = any(episode.episode_type == "finale" for episode in episodes) if episodes else False
        return completed

    def __get_tv_latest_episode(self, mediainfo: MediaInfo, season: int) -> \
            Tuple[Optional[TmdbEpisode], Optional[TmdbEpisode]]:
        """
        按季获取剧集最新播出集和下一集
        :param mediainfo: 媒体信息
        :param season: 季数
        """
        if not mediainfo or not mediainfo.tmdb_id or not season:
            return None, None

        episodes = self.tmdb_chain.tmdb_episodes(tmdbid=mediainfo.tmdb_id, season=season)
        if not episodes:
            return None, None

        now = datetime.now()
        valid_eps = []
        for ep in episodes:
            # 判断 episode_number 存在, 并能解析出有效的播出日期
            if ep.episode_number is not None and ep.air_date:
                ep_datetime, _ = self.__parse_date(ep.air_date)
                if ep_datetime:
                    valid_eps.append((ep, ep_datetime))

        if not valid_eps:
            return None, None

        # 按集数降序排序, 集数越大排在越前
        valid_eps.sort(key=lambda x: x[0].episode_number, reverse=True)

        latest_episode = None
        next_episode = None

        for i, (ep, air_dt) in enumerate(valid_eps):
            if air_dt <= now:
                latest_episode = ep
                # 如果 i-1 >= 0, 即可视为此下一集比当前集数更大
                if i - 1 >= 0:
                    next_episode = valid_eps[i - 1][0]
                break

        latest_info = f"{latest_episode.episode_number} ({latest_episode.air_date})" if latest_episode else None
        next_info = f"{next_episode.episode_number} ({next_episode.air_date})" if next_episode else None
        logger.debug(f"{mediainfo.title_year} 季{season} - latest_episode: {latest_info}, next_episode: {next_info}")
        return latest_episode, next_episode

    def __get_tv_season_air_date(self, mediainfo: MediaInfo, season: int) -> Tuple[Optional[datetime], Optional[str]]:
        """
        按季获取剧集上映日期
        :param mediainfo: 媒体信息
        :param season: 季数
        """
        # 尝试从 season_info 中获取上映日期
        air_day = None
        for season_info in mediainfo.season_info:
            if season_info.get("season_number") == season:
                air_day = season_info.get("air_date")
                break

        # 如果获取到有效的上映日期，则尝试解析后返回
        if air_day:
            air_date, air_day = self.__parse_date(air_day)
            if air_date:
                return air_date, air_day
            else:
                logger.warning(f"{mediainfo.title} 季 {season} 的上映日期格式不正确，尝试从集的详细信息中获取")
        else:
            logger.warning(f"{mediainfo.title} 未找到季 {season} 的上映日期，尝试从集的详细信息中获取")

        # 未能从 season_info 中获取有效日期时，从剧集详情中获取
        episodes = self.tmdb_chain.tmdb_episodes(tmdbid=mediainfo.tmdb_id, season=season)
        if not episodes:
            logger.warning(f"{mediainfo.title} 未找到季 {season} 的剧集信息，未能获取到上映日期")
            return None, None

        episodes = [ep for ep in episodes if ep.episode_number is not None]
        episodes.sort(key=lambda x: x.episode_number)
        for ep in episodes:
            if ep.air_date:
                ep_date, ep_day = self.__parse_date(ep.air_date)
                if ep_date:
                    return ep_date, ep_day

        logger.warning(f"{mediainfo.title} 季 {season} 未能从剧集信息中获取到上映日期")
        return None, None

    @staticmethod
    def __get_tv_season_episode_count(mediainfo: MediaInfo, season: int) -> Optional[int]:
        """
        按季获取剧集总数
        :param mediainfo: 媒体信息
        :param season: 季数
        """
        episode_count = None
        for season_info in mediainfo.season_info:
            if season_info.get("season_number") == season:
                episode_count = season_info.get("episode_count")
                continue

        if episode_count is None:
            # 未找到与订阅季数匹配的剧集总数
            logger.warning(f"{mediainfo.title} 未找到与订阅季数 {season} 对应的 episode_count")

        return episode_count

    def __get_related_download_histories(self, subscribe_id: Optional[int], subscribe: Subscribe) -> \
            List[DownloadHistory]:
        """
        获取关联的下载记录
        """
        subscribe_type = MediaType(subscribe.type)
        if subscribe_type == MediaType.TV:
            meta = self.__get_subscribe_meta(subscribe)
            downloads = self.downloadhistory_oper.get_last_by(mtype=subscribe.type, title=subscribe.name,
                                                              year=subscribe.year, season=meta.season,
                                                              tmdbid=subscribe.tmdbid)
        else:
            downloads = self.downloadhistory_oper.get_last_by(mtype=subscribe.type, title=subscribe.name,
                                                              year=subscribe.year, tmdbid=subscribe.tmdbid)

        if not downloads:
            return []

        related_downloads = []
        for download in downloads:
            # 检查 download.note 是否为字典并获取 source
            source = download.note.get("source") if isinstance(download.note, dict) else ""
            if not source:
                continue
            if download.date <= subscribe.date:
                continue
            subscribe_info = self.__get_subscribe_by_source(source=source)[0]
            if not subscribe_info:
                continue
            # 检查订阅信息是否匹配
            if subscribe_info.get("id") != subscribe_id:
                continue
            if subscribe_info.get("tmdbid") != subscribe.tmdbid:
                continue
            if subscribe_info.get("year") != subscribe.year:
                continue
            if subscribe_type == MediaType.TV:
                if subscribe_info.get("season") != subscribe.season:
                    continue
                meta_info = MetaInfo(
                    title=download.torrent_name,
                    subtitle=download.torrent_description
                )
                if meta_info.total_episode == subscribe.total_episode:
                    continue
            # 如果所有条件都满足，则将下载记录加入结果列表
            related_downloads.append(download)
        return related_downloads

    @staticmethod
    def __parse_date(day: str, f: str = "%Y-%m-%d"):
        """
        格式化日期
        """
        try:
            if not day:
                return None, None
            date = datetime.strptime(day, f)
            return date, day
        except ValueError:
            logger.error(f"day 格式错误：{day}", exc_info=True)
            return None, None

    @staticmethod
    def __get_default_tracker_response():
        """
        获取默认Tracker响应关键字
        """
        return """torrent not registered with this tracker
torrent banned"""

    @staticmethod
    def __compare_versions(version1: str, version2: str) -> int:
        """
        比较两个版本号的大小
        :param version1: version1
        :param version2: version2
        :return: 1 (version2 > version1)
               0 (version2 == version1)
              -1 (version2 < version1)
        """
        try:
            v1 = Version(version1)
            v2 = Version(version2)
            if v2 > v1:
                return 1
            elif v2 == v1:
                return 0
            else:
                return -1
        except Exception as e:
            logger.error(f"Invalid version format: {e}", exc_info=True)
            return 0
