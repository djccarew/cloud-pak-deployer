from flask import Flask, send_from_directory,request,make_response
import sys
import json
import subprocess
import os
import yaml
from shutil import copyfile
from pathlib import Path
import re
import glob

from logging.config import dictConfig

dictConfig(
    {
        "version": 1,
        "formatters": {
            "default": {
                "format": "[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "default",
            }
        },
        "root": {"level": "DEBUG", "handlers": ["console"]},
    }
)

app = Flask(__name__,static_url_path='', static_folder='ww')

parent = Path(os.path.dirname(os.path.realpath(__file__))).parent
app.logger.info('Parent path of python script: {}'.format(parent))
cp_base_config_path = os.path.join(parent,'sample-configurations/web-ui-base-config/cloud-pak')
ocp_base_config_path = os.path.join(parent,'sample-configurations/web-ui-base-config/ocp')
config_dir=str(os.getenv('CONFIG_DIR'))
status_dir=str(os.getenv('STATUS_DIR'))

Path( status_dir+'/log' ).mkdir( parents=True, exist_ok=True )
Path( config_dir+'/config' ).mkdir( parents=True, exist_ok=True )

# Global variable set in /v1/configuration
generated_config_yaml_path = ""

@app.route('/')
def index():
    return send_from_directory(app.static_folder,'index.html')

@app.route('/api/v1/deploy',methods=["POST"])
def deploy():
    body = json.loads(request.get_data())
    with open(generated_config_yaml_path, 'r', encoding='UTF-8') as f:
        content = f.read()
        docs=yaml.load_all(content, Loader=yaml.FullLoader)
        f.close()
        for doc in docs:
            if 'global_config' in doc.keys():
                global_env_id=doc['global_config']['env_id']
            if 'openshift' in doc.keys():
                openshift_name=doc['openshift'][0]['name'].replace('{{ env_id }}',global_env_id)
                break
    deployer_env = os.environ.copy()
    if body['cloud']=='ibm-cloud':
      deployer_env['IBM_CLOUD_API_KEY']=body['env']['ibmCloudAPIKey']
    deployer_env['CP_ENTITLEMENT_KEY']=body['entitlementKey']
    deployer_env['CONFIG_DIR']=config_dir
    deployer_env['STATUS_DIR']=status_dir
    app.logger.info('openshift name: {}'.format(openshift_name))
    app.logger.info('oc login command: {}'.format(body['oc_login_command']))

    # Assemble the deploy command
    deploy_command=['/cloud-pak-deployer/cp-deploy.sh']
    deploy_command+=['env','apply']
    deploy_command+=['-e=env_id={}'.format(body['envId'])]
    deploy_command+=['-vs={}-oc-login={}'.format(openshift_name, body['oc_login_command'])]
    deploy_command+=['-vvv']
    app.logger.info('deploy command: {}'.format(deploy_command))

    log = open('/tmp/cp-deploy.log', 'a')
    process = subprocess.Popen(deploy_command, 
                    stdout=log,
                    stderr=log,
                    universal_newlines=True,
                    env=deployer_env)

    return 'running'

@app.route('/api/v1/oc-login',methods=["POST"])
def oc_login():
    body = json.loads(request.get_data())
    print(body, file=sys.stderr)
    env = {}
    oc_login_command=body['oc_login_command']
    oc_login_command = oc_login_command.strip()

    pattern = r'oc(\s+)login(\s)(.*)'    
    isOcLoginCmd = re.match(pattern, oc_login_command)    

    if isOcLoginCmd : 
        result_code=os.system(oc_login_command)
        result={"code": result_code}    
        return json.dumps(result)
    else:
        return make_response('Bad Request', 400)  
    

@app.route('/api/v1/configuration',methods=["GET"])
def check_configuration():
    result = {
        "code":-1,
        "message":"",
        "data":{},
    }

    global generated_config_yaml_path

    found_config_files=glob.glob(config_dir+'/config/*.yaml')
    if len(found_config_files) == 0:
        generated_config_yaml_path = config_dir+'/config/cpd-config.yaml'
    elif len(found_config_files) > 1:
        errmsg="More than 1 yaml file found in directory {}. Wizard can be used for 0 or 1 config files.".format(config_dir+'/config')
        app.logger.error(errmsg)
        result['code'] = 400
        result['message'] = errmsg
        return result
    else:
        generated_config_yaml_path = found_config_files[0]

    app.logger.info('Config file that will be updated is {}'.format(generated_config_yaml_path))
    try:
        with open(generated_config_yaml_path, "r", encoding='UTF-8') as f:
            temp={}
            content = f.read()
            # app.logger.info(content)
            docs=yaml.safe_load_all(content)
            for doc in docs:
                temp={**temp, **doc}

            if 'cp4d' in temp:
                result['data']['cp4d']=temp['cp4d']
                del temp['cp4d']
            else:
                app.logger.info("Loading base cp4d data from {}".format(cp_base_config_path+'/cp4i.yaml'))
                result['data']['cp4d']=loadYamlFile(cp_base_config_path+'/cp4d.yaml')['cp4d']

            if 'cp4i' in temp:
                result['data']['cp4i']=temp['cp4i']
                del temp['cp4i']
            else:
                app.logger.info("Loading base cp4i data from {}".format(cp_base_config_path+'/cp4i.yaml'))
                result['data']['cp4i']=loadYamlFile(cp_base_config_path+'/cp4i.yaml')['cp4i']

            result['data']['ocp']=temp
            if 'env_id' not in result['data']['ocp']['global_config']:
                result['data']['ocp']['global_config']['env_id']='demo'
                app.logger.warning("Added env_id to global_config: {}".format(result['data']['ocp']['global_config']))

            result['code'] = 0
            result['message'] = "Successfully retrieved configuration."
            f.close()
            # app.logger.info('Result of reading file: {}'.format(result))
    except FileNotFoundError:
        result['code'] = 404
        result['message'] = "Configuration File is not found."
        app.logger.warning('Error while reading file'.format(result))
    except PermissionError:
        result['code'] = 401
        result['message'] = "Permission Error."
    except IOError:
        result['code'] = 101
        result['message'] = "IO Error."
    return result

@app.route('/api/v1/cartridges/<cloudpak>',methods=["GET"])
def getCartridges(cloudpak):
    if cloudpak not in ['cp4d', 'cp4i']:
       return make_response('Bad Request', 400)

    return loadYamlFile(cp_base_config_path+'/{}.yaml'.format(cloudpak))


@app.route('/api/v1/logs',methods=["GET"])
def getLogs():
    result={}
    result["logs"]='waiting'
    log_path=status_dir+'/log/cloud-pak-deployer.log'
    print(log_path)
    if os.path.exists(log_path):
        result["logs"]=open(log_path,"r").read()
    return json.dumps(result)

@app.route('/api/v1/region/<cloud>',methods=["GET"])
def getRegion(cloud):
   ressult={}
   with open(inventory_config_path+'/{}.inv'.format(cloud),'r') as f:
       lines = f.readlines()
       for line in lines:
         if 'ibm_cloud_region' in line:
             ressult['region'] = line.split('=')[1].replace('\n','')
             break
   return json.dumps(ressult)

def update_region(path, region):
    lines=[]
    newlines=[]
    with open(path, 'r') as f1:
       lines = f1.readlines()
       for line in lines:
          if 'ibm_cloud_region' in line:
            line = f'ibm_cloud_region={region}'
          if 'aws_region' in line:
            line = f'aws_region={region}'
          newlines.append(line)
    with open(path, 'w') as w:
         w.writelines(newlines)    

def update_storage(path, storage):
    content = ""
    with open(path, 'r') as f1:
        read_all = f1.read()
        datas = yaml.safe_load_all(read_all)
        for data in datas:
            content=content+"---\n"
            if 'openshift' in data.keys():
                   data['openshift'][0]['openshift_storage']=storage
            content=content+yaml.safe_dump(data)
    with open(path, 'w') as f:
        f.write(content)

@app.route('/api/v1/storages/<cloud>',methods=["GET"])
def getStorages(cloud):
    ocp_config=""
    with open(ocp_base_config_path+'/{}.yaml'.format(cloud), encoding='UTF-8') as f:
        read_all = f.read()

    datas = yaml.load_all(read_all, Loader=yaml.FullLoader)
    for data in datas:
        if 'openshift' in data.keys():
            ocp_config = data['openshift'][0]['openshift_storage']
            break
    return json.dumps(ocp_config)

def update_cp4d_cartridges(path, cartridges, storage, cloudpak):
    content=""
    with open(path, 'r') as f1:
        content = f1.read()
        docs=yaml.safe_load_all(content)
        for doc in docs:
            if cloudpak in doc.keys():
                doc[cloudpak][0]['cartridges']=cartridges
                doc[cloudpak][0]['openshift_storage_name']=storage
                content=yaml.safe_dump(doc)
                content = '---\n'+content
                break
    with open(path, 'w') as f1:
        f1.write(content)

def update_cp4i_cartridges(path, cartridges, storage, cloudpak):
    content=""
    with open(path, 'r') as f1:
        content = f1.read()
        docs=yaml.safe_load_all(content)
        for doc in docs:
            if cloudpak in doc.keys():
                doc[cloudpak][0]['instances']=cartridges
                doc[cloudpak][0]['openshift_storage_name']=storage
                content=yaml.safe_dump(doc)
                content = '---\n'+content
                break
    with open(path, 'w') as f1:
        f1.write(content)

def loadYamlFile(path):
    result={}
    content=""
    with open(path, 'r', encoding='UTF-8') as f1:
        content=f1.read()
        docs=yaml.safe_load_all(content)
        for doc in docs:
            result={**result, **doc}
    return result

def mergeSaveConfig(ocp_config, cp4d_config, cp4i_config):
    global generated_config_yaml_path

    ocp_yaml=yaml.safe_dump(ocp_config)
    
    all_in_one = '---\n'+ocp_yaml
    if cp4d_config!={}:
        cp4d_yaml=yaml.safe_dump(cp4d_config)
        cp4d_yaml = '\n\n'+cp4d_yaml
        all_in_one = all_in_one + cp4d_yaml
    if cp4i_config!={}:
        cp4i_yaml=yaml.safe_dump(cp4i_config)
        cp4i_yaml = '\n\n'+cp4i_yaml
        all_in_one = all_in_one + cp4i_yaml

    with open(generated_config_yaml_path, 'w', encoding='UTF-8') as f1:
        f1.write(all_in_one)
        f1.close()

    with open(generated_config_yaml_path, "r", encoding='UTF-8') as f1:
        result={}
        result["config"]=f1.read()
        f1.close()
    return json.dumps(result) 



@app.route('/api/v1/createConfig',methods=["POST"])
def createConfig():
    body = json.loads(request.get_data())
    # if not body['envId'] or not body['cloud'] or not body['cp4d'] or not body['cp4i'] or not body['storages'] or not body['cp4dLicense'] or not body['cp4iLicense'] or not body['cp4dVersion'] or not body['cp4iVersion'] or not body['CP4DPlatform'] or not body['CP4IPlatform']:
    #    return make_response('Bad Request', 400)

    env_id=body['envId']
    cloud=body['cloud']
    region=body['region']
    cp4d=body['cp4d']
    cp4i=body['cp4i']
    storages=body['storages']
    cp4dLicense=body['cp4dLicense']
    cp4iLicense=body['cp4iLicense']
    cp4dVersion=body['cp4dVersion']
    cp4iVersion=body['cp4iVersion']
    CP4DPlatform=body['CP4DPlatform']
    CP4IPlatform=body['CP4IPlatform']
    
    # Load the base yaml files
    ocp_config=loadYamlFile(ocp_base_config_path+'/{}.yaml'.format(cloud))
    cp4d_config=loadYamlFile(cp_base_config_path+'/cp4d.yaml')
    cp4i_config=loadYamlFile(cp_base_config_path+'/cp4i.yaml')

    # Update for region
    if cloud=="ibm-cloud":
        ocp_config['global_config']['ibm_cloud_region']=region
    elif cloud=="aws":
        ocp_config['global_config']['aws_region']=region

    # Update for EnvId
    ocp_config['global_config']['env_id']=env_id

    # Update for cp4d
    cp4d_selected=CP4DPlatform
    # for cartridge in cp4d:
    #     if 'state' in cartridge and cartridge['state']=='installed':
    #         cp4d_selected=True
    if cp4d_selected:
        cp4d_config['cp4d'][0]['cartridges']=cp4d
        cp4d_config['cp4d'][0]['accept_licenses']=cp4dLicense
        cp4d_config['cp4d'][0]['cp4d_version']=cp4dVersion
    else:
        cp4d_config={}
    # Update for cp4i
    cp4i_selected=CP4IPlatform
    # for instance in cp4i:
    #     if 'state' in instance and instance['state']=='installed':
    #         cp4i_selected=True
    if cp4i_selected:
        cp4i_config['cp4i'][0]['instances']=cp4i
        cp4i_config['cp4i'][0]['accept_licenses']=cp4iLicense
        cp4d_config['cp4i'][0]['cp4i_version']=cp4iVersion
    else:
        cp4i_config={}

    return mergeSaveConfig(ocp_config, cp4d_config, cp4i_config)

@app.route('/api/v1/updateConfig',methods=["PUT"])
def updateConfig():
    global generated_config_yaml_path

    body = json.loads(request.get_data())
    # if not body['cp4d'] or not body['cp4i'] or not body['cp4dLicense'] or not body['cp4iLicense'] or not body['cp4dVersion'] or not body['cp4iVersion'] or not body['CP4DPlatform'] or not body['CP4IPlatform']:
    #    return make_response('Bad Request', 400)

    cp4d_cartridges=body['cp4d']
    cp4i_instances=body['cp4i']
    cp4dLicense=body['cp4dLicense']
    cp4iLicense=body['cp4iLicense']
    cp4dVersion=body['cp4dVersion']
    cp4iVersion=body['cp4iVersion']
    CP4DPlatform=body['CP4DPlatform']
    CP4IPlatform=body['CP4IPlatform']

    with open(generated_config_yaml_path, 'r', encoding='UTF-8') as f1:
        temp={}
        cp4d_config={}
        cp4i_config={}
        ocp_config={}
        content = f1.read()
        f1.close()
        docs=yaml.safe_load_all(content)
        for doc in docs:
            temp={**temp, **doc}

        if 'cp4d' not in temp:
            temp['cp4d']=loadYamlFile(cp_base_config_path+'/cp4d.yaml')['cp4d']
        if 'cp4i' not in temp:
            temp['cp4i']=loadYamlFile(cp_base_config_path+'/cp4i.yaml')['cp4i']

        # app.logger.info("temp: {}".format(temp))

        cp4d_selected=CP4DPlatform
        # for cartridge in cp4d_cartridges:
        #     if 'state' in cartridge and cartridge['state']=='installed':
        #         cp4d_selected=True
        if cp4d_selected:
            cp4d_config['cp4d']=temp['cp4d']
            cp4d_config['cp4d'][0]['cartridges']=cp4d_cartridges
            cp4d_config['cp4d'][0]['accept_licenses']=cp4dLicense
            cp4d_config['cp4d'][0]['cp4d_version']=cp4dVersion
        del temp['cp4d']

        cp4i_selected=CP4IPlatform
        # for instance in cp4i_instances:
        #     if 'state' in instance and instance['state']=='installed':
        #         cp4i_selected=True
        if cp4i_selected:
            cp4i_config['cp4i']=temp['cp4i']
            cp4d_config['cp4i'][0]['instances']=cp4i_instances
            cp4i_config['cp4i'][0]['accept_licenses']=cp4iLicense
            cp4d_config['cp4i'][0]['cp4i_version']=cp4iVersion
        del temp['cp4i']
        
        ocp_config=temp
        if 'env_id' not in ocp_config['global_config']:
            ocp_config['global_config']['env_id']='demo'
        
    return mergeSaveConfig(ocp_config, cp4d_config, cp4i_config)

@app.route('/api/v1/saveConfig',methods=["POST"])
def saveConfig():
    body = json.loads(request.get_data())
    if not body['config']:
       return make_response('Bad Request', 400)

    config_data=body['config']

    cp4d_config={}
    cp4i_config={}
    ocp_config={}
    
    if 'cp4d' in config_data:
        cp4d_config['cp4d']=config_data['cp4d']
        del config_data['cp4d']
    if 'cp4i' in config_data:
        cp4i_config['cp4i']=config_data['cp4i']
        del config_data['cp4i']
    ocp_config=config_data

    return mergeSaveConfig(ocp_config, cp4d_config, cp4i_config)
  
if __name__ == '__main__':
    app.run(host='0.0.0.0', port='32080', debug=False)    