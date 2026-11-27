@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title JMeter 快速壓測
color 0F

echo ================================================================
echo   JMeter 快速壓測
echo ================================================================
echo.

rem ================================================================ 1. 找 JMeter
set "JMETER="
where jmeter.bat >nul 2>&1 && set "JMETER=jmeter.bat"
if not defined JMETER if defined JMETER_HOME if exist "%JMETER_HOME%\bin\jmeter.bat" set "JMETER=%JMETER_HOME%\bin\jmeter.bat"
if not defined JMETER for /d %%D in ("C:\apache-jmeter-*") do if exist "%%~fD\bin\jmeter.bat" set "JMETER=%%~fD\bin\jmeter.bat"
if not defined JMETER for /d %%D in ("C:\Program Files\apache-jmeter-*") do if exist "%%~fD\bin\jmeter.bat" set "JMETER=%%~fD\bin\jmeter.bat"
if not defined JMETER for /d %%D in ("%USERPROFILE%\apache-jmeter-*") do if exist "%%~fD\bin\jmeter.bat" set "JMETER=%%~fD\bin\jmeter.bat"
if not defined JMETER for /d %%D in ("D:\apache-jmeter-*") do if exist "%%~fD\bin\jmeter.bat" set "JMETER=%%~fD\bin\jmeter.bat"

if defined JMETER goto :JMETER_FOUND
echo [提示] 找不到 Apache JMeter，請貼上 jmeter.bat 的完整路徑
echo        例如 C:\apache-jmeter-5.6.3\bin\jmeter.bat
echo        直接按 Enter 結束。
echo.
set "JMETER="
set /p "JMETER=路徑 : "
if not defined JMETER goto :CANCEL
goto :JMETER_READY

:JMETER_FOUND
echo [OK] 找到 JMeter : !JMETER!

:JMETER_READY
set JMETER=%JMETER:"=%
if exist "!JMETER!\bin\jmeter.bat" set "JMETER=!JMETER!\bin\jmeter.bat"
if exist "!JMETER!" goto :JMETER_OK
echo [錯誤] 路徑不存在 : !JMETER!
goto :FAIL
:JMETER_OK

where java >nul 2>&1
if not errorlevel 1 goto :JAVA_OK
echo [錯誤] 找不到 java。JMeter 需要 JRE 17 以上，請安裝 JDK 或設定 JAVA_HOME。
goto :FAIL
:JAVA_OK
echo.

rem ================================================================ 2. 輸入（全部問完才解析）
:ASK_URL
echo 目標網址（可含路徑，例：https://example.com/login）
set "URL="
set /p "URL=  ^> "
if not defined URL goto :ASK_URL
set "URL=!URL: =!"
if not defined URL goto :ASK_URL
echo !URL!| findstr /c:"." >nul
if not errorlevel 1 goto :URL_OK
echo [錯誤] 網址看起來不完整，請重新輸入。
echo.
goto :ASK_URL
:URL_OK
echo.

:ASK_THREADS
set "THREADS="
echo 併發執行緒數（1 - 5000，第一次建議 10 到 50 先確認網站撐得住）
set /p "THREADS=  ^> "
if not defined THREADS goto :ASK_THREADS
echo !THREADS!| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 goto :BAD_THREADS
if !THREADS! LSS 1 goto :BAD_THREADS
if !THREADS! GTR 5000 goto :BAD_THREADS
goto :THREADS_OK
:BAD_THREADS
echo [錯誤] 請輸入 1 到 5000 之間的數字。
echo.
goto :ASK_THREADS
:THREADS_OK
echo.

:ASK_DURATION
set "DURATION="
echo 持續秒數（直接按 Enter 用 60 秒）
set /p "DURATION=  ^> "
if defined DURATION goto :DURATION_CHECK
set "DURATION=60"
:DURATION_CHECK
echo !DURATION!| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 goto :BAD_DURATION
if !DURATION! LSS 5 goto :BAD_DURATION
goto :DURATION_OK
:BAD_DURATION
echo [錯誤] 請輸入 5 以上的數字。
echo.
goto :ASK_DURATION
:DURATION_OK
set /a RAMP=!DURATION!/10
if !RAMP! LSS 1 set "RAMP=1"
echo.

rem ================================================================ 3. 確認
echo ----------------------------------------------------------------
echo   目標網址 : !URL!
echo   併發     : !THREADS! 個執行緒
echo   持續     : !DURATION! 秒（前 !RAMP! 秒爬坡）
echo   產生器   : 本機 !COMPUTERNAME!
echo   請求方式 : GET，跟隨重導向，連線逾時 10s / 回應逾時 30s
echo ----------------------------------------------------------------
echo   請確認你已取得該站台的壓測授權。對未授權的系統加壓可能觸法。
echo.
set "CONFIRM="
set /p "CONFIRM=確定開始？(Y/N) : "
if /i "!CONFIRM!"=="Y" goto :RUN
goto :CANCEL

rem ================================================================ 4. 解析網址
rem 注意：for /f 會吃掉 stdin，所以必須放在所有 set /p 之後
:RUN
set "SCHEME=https"
set "REST=!URL!"
if /i not "!REST:~0,8!"=="https://" goto :CHECK_HTTP
set "REST=!REST:~8!"
goto :URL_PARSED
:CHECK_HTTP
if /i not "!REST:~0,7!"=="http://" goto :URL_PARSED
set "SCHEME=http"
set "REST=!REST:~7!"
:URL_PARSED

set "HOSTPORT=!REST!"
set "REQPATH=/"
for /f "tokens=1,* delims=/" %%a in ("!REST!") do (
  set "HOSTPORT=%%a"
  if not "%%b"=="" set "REQPATH=/%%b"
)

set "HOST="
set "PORT="
for /f "tokens=1,2 delims=:" %%a in ("!HOSTPORT!") do (
  set "HOST=%%a"
  if not "%%b"=="" set "PORT=%%b"
)

if defined HOST goto :HOST_OK
echo [錯誤] 無法解析網址 : !URL!
goto :FAIL
:HOST_OK
if defined PORT goto :PORT_OK
if /i "!SCHEME!"=="https" set "PORT=443"
if /i "!SCHEME!"=="http" set "PORT=80"
:PORT_OK

rem ================================================================ 5. 執行
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%i"
if defined STAMP goto :STAMP_OK
set "STAMP=run"
:STAMP_OK
set "RUNDIR=%~dp0runs\!STAMP!"
mkdir "!RUNDIR!" 2>nul
if exist "!RUNDIR!" goto :RUNDIR_OK
echo [錯誤] 無法建立輸出資料夾 : !RUNDIR!
goto :FAIL
:RUNDIR_OK
set "JMX=!RUNDIR!\plan.jmx"

>>"%JMX%" echo ^<?xml version="1.0" encoding="UTF-8"?^>
>>"%JMX%" echo ^<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3"^>
>>"%JMX%" echo   ^<hashTree^>
>>"%JMX%" echo     ^<TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="Quick Load Test" enabled="true"^>
>>"%JMX%" echo       ^<stringProp name="TestPlan.comments"^>^</stringProp^>
>>"%JMX%" echo       ^<boolProp name="TestPlan.functional_mode"^>false^</boolProp^>
>>"%JMX%" echo       ^<boolProp name="TestPlan.serialize_threadgroups"^>false^</boolProp^>
>>"%JMX%" echo       ^<elementProp name="TestPlan.user_defined_variables" elementType="Arguments" guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true"^>
>>"%JMX%" echo         ^<collectionProp name="Arguments.arguments"/^>
>>"%JMX%" echo       ^</elementProp^>
>>"%JMX%" echo       ^<stringProp name="TestPlan.user_define_classpath"^>^</stringProp^>
>>"%JMX%" echo     ^</TestPlan^>
>>"%JMX%" echo     ^<hashTree^>
>>"%JMX%" echo       ^<ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="Threads" enabled="true"^>
>>"%JMX%" echo         ^<stringProp name="ThreadGroup.on_sample_error"^>continue^</stringProp^>
>>"%JMX%" echo         ^<elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true"^>
>>"%JMX%" echo           ^<boolProp name="LoopController.continue_forever"^>true^</boolProp^>
>>"%JMX%" echo           ^<stringProp name="LoopController.loops"^>-1^</stringProp^>
>>"%JMX%" echo         ^</elementProp^>
>>"%JMX%" echo         ^<stringProp name="ThreadGroup.num_threads"^>${__P(threads,10)}^</stringProp^>
>>"%JMX%" echo         ^<stringProp name="ThreadGroup.ramp_time"^>${__P(ramp,10)}^</stringProp^>
>>"%JMX%" echo         ^<boolProp name="ThreadGroup.scheduler"^>true^</boolProp^>
>>"%JMX%" echo         ^<stringProp name="ThreadGroup.duration"^>${__P(duration,60)}^</stringProp^>
>>"%JMX%" echo         ^<stringProp name="ThreadGroup.delay"^>0^</stringProp^>
>>"%JMX%" echo         ^<boolProp name="ThreadGroup.same_user_on_next_iteration"^>true^</boolProp^>
>>"%JMX%" echo       ^</ThreadGroup^>
>>"%JMX%" echo       ^<hashTree^>
>>"%JMX%" echo         ^<HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="HTTP Request" enabled="true"^>
>>"%JMX%" echo           ^<elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true"^>
>>"%JMX%" echo             ^<collectionProp name="Arguments.arguments"/^>
>>"%JMX%" echo           ^</elementProp^>
>>"%JMX%" echo           ^<stringProp name="HTTPSampler.domain"^>${__P(host,localhost)}^</stringProp^>
>>"%JMX%" echo           ^<stringProp name="HTTPSampler.port"^>${__P(port,443)}^</stringProp^>
>>"%JMX%" echo           ^<stringProp name="HTTPSampler.protocol"^>${__P(protocol,https)}^</stringProp^>
>>"%JMX%" echo           ^<stringProp name="HTTPSampler.contentEncoding"^>UTF-8^</stringProp^>
>>"%JMX%" echo           ^<stringProp name="HTTPSampler.path"^>${__P(path,/)}^</stringProp^>
>>"%JMX%" echo           ^<stringProp name="HTTPSampler.method"^>GET^</stringProp^>
>>"%JMX%" echo           ^<boolProp name="HTTPSampler.follow_redirects"^>true^</boolProp^>
>>"%JMX%" echo           ^<boolProp name="HTTPSampler.auto_redirects"^>false^</boolProp^>
>>"%JMX%" echo           ^<boolProp name="HTTPSampler.use_keepalive"^>true^</boolProp^>
>>"%JMX%" echo           ^<boolProp name="HTTPSampler.DO_MULTIPART_POST"^>false^</boolProp^>
>>"%JMX%" echo           ^<stringProp name="HTTPSampler.embedded_url_re"^>^</stringProp^>
>>"%JMX%" echo           ^<stringProp name="HTTPSampler.connect_timeout"^>10000^</stringProp^>
>>"%JMX%" echo           ^<stringProp name="HTTPSampler.response_timeout"^>30000^</stringProp^>
>>"%JMX%" echo         ^</HTTPSamplerProxy^>
>>"%JMX%" echo         ^<hashTree/^>
>>"%JMX%" echo         ^<HeaderManager guiclass="HeaderPanel" testclass="HeaderManager" testname="Headers" enabled="true"^>
>>"%JMX%" echo           ^<collectionProp name="HeaderManager.headers"^>
>>"%JMX%" echo             ^<elementProp name="" elementType="Header"^>
>>"%JMX%" echo               ^<stringProp name="Header.name"^>User-Agent^</stringProp^>
>>"%JMX%" echo               ^<stringProp name="Header.value"^>JMeter-QuickTest/1.0^</stringProp^>
>>"%JMX%" echo             ^</elementProp^>
>>"%JMX%" echo             ^<elementProp name="" elementType="Header"^>
>>"%JMX%" echo               ^<stringProp name="Header.name"^>Accept-Encoding^</stringProp^>
>>"%JMX%" echo               ^<stringProp name="Header.value"^>gzip, deflate^</stringProp^>
>>"%JMX%" echo             ^</elementProp^>
>>"%JMX%" echo           ^</collectionProp^>
>>"%JMX%" echo         ^</HeaderManager^>
>>"%JMX%" echo         ^<hashTree/^>
>>"%JMX%" echo       ^</hashTree^>
>>"%JMX%" echo     ^</hashTree^>
>>"%JMX%" echo   ^</hashTree^>
>>"%JMX%" echo ^</jmeterTestPlan^>

echo.
echo ================================================================
echo   目標 : !SCHEME!://!HOST!:!PORT!!REQPATH!
echo   開始壓測，請勿關閉視窗（Ctrl+C 可中止）
echo ================================================================
echo.

call "!JMETER!" -n -t "!JMX!" -l "!RUNDIR!\result.jtl" -e -o "!RUNDIR!\report" ^
  -Jprotocol=!SCHEME! -Jhost=!HOST! -Jport=!PORT! -Jpath="!REQPATH!" ^
  -Jthreads=!THREADS! -Jramp=!RAMP! -Jduration=!DURATION!

set "RC=!errorlevel!"
echo.
if "!RC!"=="0" goto :DONE
echo [錯誤] JMeter 結束碼 !RC!，請看上面的錯誤訊息。
echo        常見原因：Java 版本不符、計畫檔被防毒攔截、目標站台無法連線。
goto :FAIL

:DONE
echo ================================================================
echo   完成
echo ================================================================
echo   原始資料 : !RUNDIR!\result.jtl
if not exist "!RUNDIR!\report\index.html" goto :NO_REPORT
echo   HTML 報告: !RUNDIR!\report\index.html
start "" "!RUNDIR!\report\index.html"
goto :SUMMARY_HINT
:NO_REPORT
echo   HTML 報告: 未產生（JMeter 3.0 以上才有）
:SUMMARY_HINT
echo.
echo 上面 summary 那一行就是總成績：
echo   Avg 平均延遲 / Min 最快 / Max 最慢 / Err 錯誤率
echo.
pause
exit /b 0

:CANCEL
echo.
echo 已取消，未產生任何流量。
timeout /t 2 >nul
exit /b 0

:FAIL
echo.
pause
exit /b 1
