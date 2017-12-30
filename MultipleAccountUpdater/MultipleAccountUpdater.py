# encoding: UTF-8
'''
读取账户资金、持仓、委托信息，保存至本地
加入守护者进程，自动开关
支持多账户
'''
import multiprocessing
from PyQt5.QtWidgets import QApplication
from datetime import datetime, time
import json
import os
import logging
from py_ctp.ctp_api import  *
from py_ctp.eventEngine import  *
import functools

# 配置日志
path = 'log/Log{date}'.format(date=datetime.now().strftime('%Y-%m-%d'))
logging.basicConfig(filename=path, level=logging.INFO)


ONEDRIVE_DIR = 'C:/OneDrive/' # 存放生成的记录的目录
# ONEDRIVE_DIR = 'X:/db_werobot/'
# 设置文件
CONFIG_FILE = 'config.json'
# 时间
NIGHT_START = time(20, 47) # 夜盘开盘
NIGHT_END = time(23, 59, 59) 
MORNING_START = time(0, 0)  # 夜盘跨过0点，分两段
MORNING_END = time(2, 31)
DAY_START = time(8, 47) # 日盘开盘
DAY_END = time(15, 35) # 日盘收盘

def stand_alone(func):
    '''
    装饰器
    如果已经有实例在跑则退出
    :return:
    '''
    @functools.wraps(func)
    def f(*args,**kwargs):
        import socket
        try:
            # 全局属性，否则变量会在方法退出后被销毁
            global soket_bind
            soket_bind = socket.socket()
            host = socket.gethostname()
            soket_bind.bind((host, 7788))
        except:
            print('已经运行一个实例，不能重复打开')
            return None
        return func(*args,**kwargs)
    return f

@stand_alone
class MainEngine:
    """主引擎，负责对API的调度"""
    #----------------------------------------------------------------------
    def __init__(self, config):
        """Constructor"""
        self.ee = EventEngine()         # 创建事件驱动引擎
        self.ee.start()                 # 启动事件驱动引擎
        self.userID = ''          # 账号
        self.password = ''        # 密码
        self.brokerID = ''        # 经纪商代码
        self.TdIp = ''         # 交易服务器地址

        self.set_up(config) # 设置账号

        # 循环查询持仓和账户相关
        self.todayBalance = []
        self.countGet = 0  # 查询延时计数
        self.lastGet = 'Position'  # 上次查询的性质，先查询账户
        # 统计净值
        self.navCalculated = False
        self.navComfirmed = False

        #持仓和账户、委托
        self.ee.register((EVENT_START + self.userID), self.startReq) # 开始查询
        self.ee.register((EVENT_ACCOUNT + self.userID), self.account)
        self.ee.register(EVENT_POSITION + self.userID, self.position)
        self.ee.register((EVENT_ORDER + self.userID), self.updateOrder)
        self.ee.register(EVENT_LOG, self.print_log)
        self.td = CtpTdApi(self, self.ee)      # 创建交易API接口
        #持仓、账户、委托、净值数据
        self.dict_account ={}
        self.dict_position ={}
        self.orderDict ={}
        self.workingOrderDict = {}
        self.nav = {}

        self.clear_history()         # 开启软件时清空一次委托记录

    def __del__(self):
        self.ee.unregister(EVENT_TIMER, self.getAccountPosition) 
        self.ee.unregister(EVENT_ACCOUNT + self.userID, self.account)
        self.ee.unregister(EVENT_POSITION + self.userID, self.position)
        self.ee.unregister(EVENT_ORDER + self.userID, self.updateOrder)
        self.ee.stop() # 停止事件驱动引擎
    #----------------------------------------------------------------------
    def login(self):
        """登陆"""
        self.td.connect(self.userID, self.password, self.brokerID, self.TdIp)
        self.put_log('用户%s已登录' % self.userID)
        # 登录时清空委托列表
        self.orderDict = {}
    #----------------------------------------------------------------------
    def print_log(self, event):
        log = event.dict_['log']
        print(log)
        t = datetime.now().strftime('%Y-%m-%d %H:%M:%S ')
        log = ''.join([t, log])
        logging.info(log)
    #----------------------------------------------------------------------
    def put_log(self, log):
        event = Event()
        event.dict_['log'] = log
        event.type_ = EVENT_LOG
        self.ee.put(event)
    #----------------------------------------------------------------------
    def set_up(self, config):
        '''set up args'''
        self.userID = config['userID']          # 账号
        self.password = config['password']         # 密码
        self.brokerID = config['brokerID']        # 经纪商代码
        self.TdIp = config['TdIp']         # 交易服务器地址

        # 当天重启软件时载入之前的资金记录，顺便加在这里 /摊手
        try:
            path = os.path.join(ONEDRIVE_DIR, self.userID + '/balance.json')
            with open(path, 'r', encoding="utf-8") as f:
                historyBalance = json.load(f)
        except FileNotFoundError:
            return
        if len(historyBalance) > 0:
            lastUpdatetime = historyBalance[0]['updateTime']
            lastUpdatetime = datetime.strptime(lastUpdatetime, '%Y-%m-%d_%H:%M:%S')
            t = datetime.now()
            if t - lastUpdatetime < timedelta(hours=6): # 如果当天出错或重启，不清空盘中资金的数据
                self.todayBalance = historyBalance
    #----------------------------------------------------------------------
    def startReq(self, event):
        self.ee.register(EVENT_TIMER, self.getAccountPosition) # 定时器事件，循环查询
        self.ee.unregister(EVENT_START + self.userID, self.startReq) 
        self.put_log(''.join([self.userID, '开始查询']))
    #----------------------------------------------------------------------
    def clear_history(self):
        self.save_nontrade()
        self.save_order()

    def save_balance(self):
        path = os.path.join(ONEDRIVE_DIR, self.userID + '/balance.json')
        with open(path, 'w', encoding="utf-8") as f:
            jsonD = json.dumps(self.todayBalance, indent=4)
            f.write(jsonD)
    def save_order(self):
        path = os.path.join(ONEDRIVE_DIR, self.userID + '/order.json')
        with open(path, 'w', encoding="utf-8") as f:
            data = json.dumps(self.orderDict, indent=4)
            f.write(data)
    def save_nontrade(self):
        path = os.path.join(ONEDRIVE_DIR, self.userID + '/nontrade.json')
        with open(path, 'w', encoding="utf-8") as f:
            data = json.dumps(self.workingOrderDict, indent=4)
            f.write(data)
    def save_nav(self):
        path = os.path.join(ONEDRIVE_DIR, self.userID + '/nav.json')
        with open(path, 'w', encoding="utf-8") as f:
            data = json.dumps(self.nav, indent=4)
            f.write(data)
    #----------------------------------------------------------------------
    def getAccountPosition(self, event):
        """循环查询账户和持仓"""
        self.countGet += 1
        # 每n秒发一次查询
        if self.countGet > 8:
            self.countGet = 0  # 清空计数

            if self.lastGet == 'Account':
                self.getPosition()
                self.lastGet = 'Position'
            else:
                self.getAccount()
                self.lastGet = 'Account'
    # ----------------------------------------------------------------------
    def account(self, event):# 处理账户事件数据
        var = event.dict_['data']
        balance = int(var["Balance"])
        dw = int(var["Deposit"] - var["Withdraw"])
        # 保存权益，供收盘时自动发送净值
        t = datetime.now().time()
        # 只记录有交易的时间段
        trading = time(20, 59) < t < time(23, 59, 59) or time(0, 0) < t < time(1, 31) or time(8, 59) < t < time(11, 31) or time(13, 29)< t < time(15, 1)
        if trading:
            updateTime = datetime.now().strftime('%Y-%m-%d_%H:%M:%S')
            data = {
            'balance': balance,
            'updateTime': updateTime,
            'DepositWithdraw': dw,
            }
            self.todayBalance.append(data)
            self.save_balance()

        onClose = time(15, 1) < t < time(15, 2)
        onComfirm = time(15, 31) < t < time(15, 32)
        if onComfirm: 
            # 停止出入金后确认出入金情况
            self.todayBalance[-1]['DepositWithdraw'] = dw
            self.save_balance()

        if onClose and not self.navCalculated:
            # 净值统计任务
            self.calculate_nav()
            self.navCalculated = True

        if onComfirm and not self.navComfirmed:
            # 净值确认任务
            self.calculate_nav()
            self.navComfirmed = True
    # ----------------------------------------------------------------------
    def position(self, event):#处理持仓事件数据
        var = event.dict_['data']
        last = event.dict_['last']
        ExchangeID = var['ExchangeID']
        directionmap = {'多持':DIRECTION_LONG, '空持':DIRECTION_SHORT}
        if var["Position"] != 0:#有持仓
            index = var["InstrumentID"] + '.' + var["PosiDirection"]
            if index not in self.dict_position.keys():#计算持仓数据
                tmp={}
                tmp["合约名称"] = var["InstrumentID"]
                tmp["持仓方向"] = var["PosiDirection"]
                tmp["总持仓量"] = var["Position"]
                tmp["今持仓量"] = 0
                tmp["昨持仓量"] = 0
                if var["PosiDirection"] == DIRECTION_LONG:
                    tmp["持仓方向"] = '多头'
                    po = round(var["PositionProfit"] + var["PositionCost"] - var["OpenCost"], 2)
                    tmp["开仓盈亏"]  = po
                else:
                    tmp["持仓方向"] = '空头'
                    po = round(var["PositionProfit"] + var["OpenCost"] - var["PositionCost"], 2)
                    tmp["开仓盈亏"]  = po
                # 上期所品种今仓昨仓分两条推送
                if ExchangeID == EXCHANGE_SHFE:
                    if var["PositionDate"] == "2":   #1今仓，2昨仓
                        tmp["昨持仓量"] = var["Position"]
                    if var["PositionDate"] == "1":  #1今仓，2昨仓
                        tmp["今持仓量"] = var["Position"]
                    pt = tmp["今持仓量"] + tmp["昨持仓量"]
                    tmp["总持仓量"] = pt
                self.dict_position[index] = tmp
            else:#更新可能会变的数据
                self.dict_position[index]["总持仓量"] = var["Position"]
                if var["PosiDirection"] == DIRECTION_LONG:
                    po = round(var["PositionProfit"] + var["PositionCost"] - var["OpenCost"], 2)
                    self.dict_position[index]["开仓盈亏"] = po
                else:
                    po = round(var["PositionProfit"] + var["OpenCost"] - var["PositionCost"], 2)
                    self.dict_position[index]["开仓盈亏"] = po
                if ExchangeID == EXCHANGE_SHFE:
                    if var["PositionDate"] == "2":   #1今仓，2昨仓
                        self.dict_position[index]["昨持仓量"] = var["Position"]
                    if var["PositionDate"] == "1":  #1今仓，2昨仓
                        self.dict_position[index]["今持仓量"] = var["Position"]
                    pt = self.dict_position[index]["今持仓量"] + self.dict_position[index]["昨持仓量"]
                    self.dict_position[index]["总持仓量"] = pt
        else :#没有持仓，有2个情况：1，已经全部平仓，2，有开仓挂单
            index = var["InstrumentID"] + '.' + var["PosiDirection"]
            if index in self.dict_position.keys():#只处理全部平仓的表格
                self.dict_position.pop(index)
        # 保存持仓数据到文件
        if last:
            path = os.path.join(ONEDRIVE_DIR, self.userID + '/position.json')
            with open(path, 'w', encoding="utf-8") as f:
                jsonD = json.dumps(self.dict_position, indent=4)
                f.write(jsonD)
    # ----------------------------------------------------------------------
    def updateOrder(self, event):
        """"""
        var = event.dict_['data']
        index = str(var["OrderLocalID"])+'.'+var["InstrumentID"]
        # 更新委托单
        if index not in self.orderDict.keys(): 
            self.orderDict[index] = {}
            self.orderDict[index]["合约代码"] =  (str(var["InstrumentID"]))
            self.orderDict[index]["时间"] = str(var["InsertTime"])
            self.orderDict[index]["价格"] = (str(var["LimitPrice"]))
            self.orderDict[index]["数量"] = (str(var["VolumeTotalOriginal"]))
            self.orderDict[index]["状态信息"] = (str(var["OrderStatus"]))
            if var["CombOffsetFlag"] == OFFSET_OPEN:
                if var["Direction"] == DIRECTION_LONG:
                    self.orderDict[index]["买卖开平标志"] = ('多开')
                else:
                    self.orderDict[index]["买卖开平标志"] = ('空开')
            else:
                if var["Direction"] == DIRECTION_SHORT:
                    self.orderDict[index]["买卖开平标志"] = ('多平')
                else:
                    self.orderDict[index]["买卖开平标志"] = ('空平')
        # 更新可能变化的数据
        else:
            self.orderDict[index]["状态信息"] = str(var["OrderStatus"])
        # 保存到本地
        self.save_order()

        # 更新可撤单    
        if index not in self.workingOrderDict.keys() and var["StatusMsg"] == '未成交':
            self.workingOrderDict[index] = {}
            # 准备保存到本地的未成交信息
            self.workingOrderDict[index]["时间"] = str(var["InsertTime"])
            self.workingOrderDict[index]["合约代码"] =  (str(var["InstrumentID"]))
            self.workingOrderDict[index]["价格"] = (str(var["LimitPrice"]))
            self.workingOrderDict[index]["数量"] = (str(var["VolumeTotalOriginal"]))
            self.workingOrderDict[index]["状态信息"] = (str(var["StatusMsg"]))
            if var["CombOffsetFlag"] == OFFSET_OPEN:
                if var["Direction"] == DIRECTION_LONG:
                    self.workingOrderDict[index]["买卖开平标志"] = ('多开')
                else:
                    self.workingOrderDict[index]["买卖开平标志"] = ('空开')
            else:
                if var["Direction"] == DIRECTION_SHORT:
                    self.workingOrderDict[index]["买卖开平标志"] = ('多平')
                else:
                    self.workingOrderDict[index]["买卖开平标志"] = ('空平')
        # 更新状态
        if index in self.workingOrderDict.keys():
            self.workingOrderDict[index]["状态信息"] = str(var["StatusMsg"])
            if var["StatusMsg"] == '全部成交':
                self.workingOrderDict.pop(index) # 删除已成交
        # 保存委托信息到本地
        self.save_nontrade()
    # ----------------------------------------------------------------------
    def getAccount(self):
        """查询账户"""
        self.td.qryAccount()
    # ----------------------------------------------------------------------
    def getPosition(self):
        """查询持仓"""
        self.td.qryPosition()        
    # ----------------------------------------------------------------------
    def calculate_nav(self):
        """计算每日净值"""
        # 载入净值历史
        self.put_log('正在统计%s的净值' % self.userID)
        first = False
        try:
            path = os.path.join(ONEDRIVE_DIR, self.userID + '/nav.json')
            with open(path, 'r', encoding="utf-8") as f:
                na = json.load(f)
        except FileNotFoundError:
            na = []
            first = True 
        # 载入当日收盘权益
        ba = self.todayBalance
        # 计算净值
        currentDate = datetime.now().strftime('%Y-%m-%d')
        balance = ba[-1]["balance"]
        dw = ba[-1]["DepositWithdraw"]
        if first:
            changeValue = 0
            totalChange = 0
        else:# 去重复
            if na[-1]['date'] == currentDate:
                na.pop(-1)
            lastBalance = na[-1]['balance']
            totalChange = na[-1]['totalChange']
            changeValue = balance - lastBalance - dw
            totalChange += changeValue
            
        change = "涨" if changeValue >= 0 else "跌"
        changeValue = abs(changeValue)
        data = {
            "date": currentDate,
            "balance": balance,
            "change": change,
            "change_value": changeValue,
            "totalChange": totalChange,
        }
        na.append(data)
        # 保存净值历史
        self.nav = na
        self.save_nav()
      
        msg = '截至%s，您的账户权益为%d，较前一交易日%s%d，账户累计盈亏%d。' % (currentDate, balance, change, changeValue, totalChange)
        self.put_log(msg)

def run_mainengine(config):
    import sys
    app = QApplication(sys.argv)
    connects = {}
    for i in range(len(config)): # 将多个账户登录分别实例化
        setting = config[i]
        connects[i] = MainEngine(setting)
        connects[i].login()
    app.exec_()   
    
class Watcher:
    '''守护进程'''
    def __init__(self):
        """constractor"""
        self.p = None # 子进程
        self.tradeDateList = [] # 交易日列表
        self.todaySetting = []
        self.currentTime = datetime.now().time() # 当前时间
        self.currentDate = ''   # 当前日期
        self.count = 0 # 循环计数
        self.config = [] # 账户信息
        self.load_tradedate() # 载入交易日列表
        self.load_config() # 载入账户信息
        self.loop() # 运行主循环

    def __def__(self):
        '''join process when exit'''
        if self.p:
            self.p.terminate()
            self.p.join()
            self.p = None
            
    def load_tradedate(self):
        """load tradeday from json"""
        with open('tradeDate.json', 'r', encoding="utf-8") as f:
            self.tradeDateList = json.load(f)    

    def load_config(self):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        # 创建新目录
        savePath = os.path.abspath(ONEDRIVE_DIR)
        listPath = [x for x in os.listdir(savePath) if os.path.isdir(os.path.join(savePath, x))]
        for index in self.config:
            id = index['userID']
            if id not in listPath:
                newPath = ''.join([savePath, id])
                os.mkdir(newPath)
                print('正在为新用户{user}创建目录'.format(user=id))

    def check_and_run(self):
        '''根据交易日列表管理子进程'''
        # 在交易日列表中查找当前日期
        i = 0
        while i < len(self.tradeDateList):
            if self.tradeDateList[i][0] == self.currentDate:
                n = i
                i = len(self.tradeDateList)
            i += 1
        # 交易日列表更新提醒
        try:
            if i - n < 10:
                print('交易日列表急需更新，否则将会在%s天后过期！' % (i-n))
        # 如果交易日列表中找不到当前日期，则终止循环
        except:
            print('马上更新交易日列表！')
            return
        # 当天的开盘时间情况
        self.todaySetting = self.tradeDateList[n]
        # 运行
        self.run_account_updater()
            
    def run_account_updater(self):
        '''运行账户查询子进程'''
        running = False
        istradeday = self.todaySetting[2]
        open_at_night = self.todaySetting[3]
        open_at_morning = self.todaySetting[4]

        # 判断是否有开盘，且在开盘时间
        if istradeday and open_at_night and (NIGHT_START < self.currentTime < NIGHT_END):
            running = True
        if open_at_morning and (MORNING_START < self.currentTime < MORNING_END):# 周六凌晨可能有开盘，因此不判断是否交易日
            running = True
        if istradeday and (DAY_START < self.currentTime < DAY_END):
            running = True

        # 交易时间启动子进程
        if running and self.p is None:
            log = ','.join([self.currentDate, self.currentTime.strftime('%H:%M:%S'), u'开始运行子进程: MultipleAccountUpdater'])
            print(log)
            self.p = multiprocessing.Process(target=run_mainengine, args=(self.config,))
            self.p.start()

        # 非交易时间则退出子进程
        if not running and self.p is not None:
            log = ','.join([self.currentDate, self.currentTime.strftime('%H:%M:%S'), u'停止运行子进程: MultipleAccountUpdater'])
            print(log)
            self.p.terminate()
            self.p.join()
            self.p = None
            
    def loop(self):
        '''定时轮询'''
        print('启动中...')
        while True:
            # 更新当前时间
            self.currentDate = datetime.now().strftime('%Y%m%d')
            self.currentTime = datetime.now().time()
            if self.currentTime >= NIGHT_END: # 避免零点重启
                sleep(1)
                continue
            self.check_and_run()
            sleep(9)
            
# 直接运行脚本可以进行测试
if __name__ == '__main__':
    watcher = Watcher()
